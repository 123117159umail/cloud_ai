import time
import requests
import paramiko
import subprocess
import os
import logging
from google.cloud import compute_v1
from google.api_core.exceptions import NotFound, Conflict, GoogleAPICallError
from dotenv import load_dotenv
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Constants
MAX_SSH_RETRIES = 5
SSH_RETRY_DELAY = 10

# Load configuration from .env
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GCP_PROJECT = os.getenv("GCP_PROJECT", "buoyant-algebra-444004-t2")
SSH_KEY_PATH = os.path.expanduser("~/.ssh/cloud_vm_key")
SSH_PUB_KEY_PATH = f"{SSH_KEY_PATH}.pub"
GCP_ZONE = "us-central1-a"
INSTANCE_NAME = "interactive-ai-vm"
MACHINE_TYPE = "n2-standard-32"
IMAGE_FAMILY = "ubuntu-2004-lts"
IMAGE_PROJECT = "ubuntu-os-cloud"

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@app.route('/')
def home():
    return "Flask Backend is Running!", 200



@app.route('/run-command', methods=['POST', 'OPTIONS'])
def run_command():
    if request.method == "OPTIONS":
        return jsonify({'status': 'ok'}), 200  # Preflight request

    data = request.json
    user_input = data.get('command')

    if not user_input:
        return jsonify({'error': 'No command provided'}), 400

    try:
        # Run the command and capture output
        result = subprocess.run(user_input, shell=True, text=True, capture_output=True)
        return jsonify({'output': result.stdout, 'error': result.stderr})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def generate_ssh_key():
    """Generate SSH key pair with secure permissions."""
    if not os.path.exists(SSH_KEY_PATH):
        logger.info("Generating new SSH key pair...")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", SSH_KEY_PATH, "-N", ""],
            check=True
        )
        os.chmod(SSH_KEY_PATH, 0o600)
        os.chmod(SSH_PUB_KEY_PATH, 0o644)
        logger.info(f"SSH key pair generated at {SSH_KEY_PATH}")
    else:
        logger.info("Using existing SSH key pair")

def get_existing_vm_ip():
    """Check for existing VM and return its IP if available."""
    try:
        instance_client = compute_v1.InstancesClient()
        instance = instance_client.get(
            project=GCP_PROJECT,
            zone=GCP_ZONE,
            instance=INSTANCE_NAME
        )
        return instance.network_interfaces[0].access_configs[0].nat_i_p
    except NotFound:
        return None

def create_gcp_vm():
    """Create GCP VM instance and return its external IP."""
    logger.info("Initializing VM creation...")
    instance_client = compute_v1.InstancesClient()
    
    with open(SSH_PUB_KEY_PATH, "r") as key_file:
        ssh_key = key_file.read().strip()

    config = {
        "name": INSTANCE_NAME,
        "machine_type": f"zones/{GCP_ZONE}/machineTypes/{MACHINE_TYPE}",
        "disks": [{
            "boot": True,
            "initialize_params": {
                "source_image": f"projects/{IMAGE_PROJECT}/global/images/family/{IMAGE_FAMILY}",
                "disk_size_gb": "50"
            },
        }],
        "network_interfaces": [{
            "access_configs": [{"name": "External NAT", "type": "ONE_TO_ONE_NAT"}]
        }],
        "metadata": {
            "items": [{"key": "ssh-keys", "value": f"ubuntu:{ssh_key}"}]
        },
        "tags": {
            "items": ["http-server", "https-server"]
        }
    }

    try:
        operation = instance_client.insert(
            project=GCP_PROJECT,
            zone=GCP_ZONE,
            instance_resource=config
        )
        operation.result()
        logger.info("VM created successfully")
        return get_existing_vm_ip()
    except Conflict:
        logger.warning("VM already exists, using existing instance")
        return get_existing_vm_ip()
    except GoogleAPICallError as e:
        logger.error(f"GCP API error: {e.message}")
        raise

def wait_for_ssh(ip_address):
    """Wait until SSH becomes available on the VM."""
    logger.info("Waiting for SSH to become available...")
    for _ in range(MAX_SSH_RETRIES):
        try:
            with paramiko.SSHClient() as ssh:
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                private_key = paramiko.Ed25519Key(filename=SSH_KEY_PATH)
                ssh.connect(ip_address, username='ubuntu', pkey=private_key, timeout=15)
                return True
        except Exception:
            time.sleep(SSH_RETRY_DELAY)
    raise ConnectionError("Failed to establish SSH connection after multiple attempts")
def interpret_command(user_input, retries=3, delay=5):
    """Uses DeepSeek AI to generate Linux shell commands with retry logic."""
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Only respond with valid Linux shell commands. Do not include explanations."},
            {"role": "user", "content": f"Generate Linux shell commands for this task: {user_input}."}
        ],
        "temperature": 0.7
    }

    for attempt in range(retries):
        try:
            response = requests.post(url, json=data, headers=headers, timeout=15)
            response.raise_for_status()  # Raise exception for HTTP errors
            result = response.json()

            if "choices" in result:
                return result["choices"][0]["message"]["content"].strip()
            else:
                return "echo 'AI failed to generate command'"

        except requests.exceptions.Timeout:
            logger.error(f"DeepSeek API request timed out. Retrying ({attempt+1}/{retries})...")
            time.sleep(delay)
        except requests.exceptions.RequestException as e:
            logger.error(f"DeepSeek API request failed: {e}")
            return "echo 'API request failed'"

    return "echo 'Command generation failed after multiple attempts'"

def clean_commands(ai_response):
    """Sanitize and extract commands from AI response."""
    cleaned = re.sub(r"(?i)^\s*```(?:bash|shell)?\s*|\s*```\s*$", "", ai_response, flags=re.MULTILINE)
    return "\n".join(line.strip() for line in cleaned.splitlines() if line.strip() and not line.startswith("#"))

def execute_command(vm_ip, command, timeout=10):
    """Execute command on VM with a timeout."""
    logger.info(f"Executing command: {command}")
    try:
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            private_key = paramiko.Ed25519Key(filename=SSH_KEY_PATH)
            ssh.connect(vm_ip, username='ubuntu', pkey=private_key, timeout=30)

            stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)

            exit_status = stdout.channel.recv_exit_status()
            output = stdout.read().decode().strip()
            error = stderr.read().decode().strip()

            if exit_status != 0:
                logger.error(f"Command failed with status {exit_status}")
                if error:
                    logger.error(f"Error output: {error}")
                    return handle_command_error(vm_ip, error)
            else:
                logger.info(f"Command output:\n{output if output else 'No output'}")
                return output

    except paramiko.SSHException as e:
        logger.error(f"SSH connection failed: {e}")
        return False

def handle_command_error(vm_ip, error_message):
    """Handle command errors using AI suggestions."""
    logger.info("Attempting to diagnose error...")
    diagnosis = debug_errors(error_message)
    print(f"\nAI Diagnosis:\n{diagnosis}")
    
    choice = input("Would you like to attempt automatic repair? (y/n): ").lower()
    if choice == 'y':
        fix_command = interpret_command(f"Fix this error: {error_message}")
        print(f"\nAI Suggested Fix: {fix_command}")
        confirm = input("Execute this fix? (y/n): ").lower()
        if confirm == 'y':
            return execute_command(vm_ip, fix_command)
    return False

def clean_commands(ai_response):
    """Sanitize and extract valid Linux commands from AI response."""
    cleaned = re.sub(r"(?i)^\s*```(?:bash|shell)?\s*|\s*```\s*$", "", ai_response, flags=re.MULTILINE)
    return "\n".join(line.strip() for line in cleaned.splitlines() if line.strip() and not line.startswith("#"))

def debug_errors(error_message):
    """Get error diagnosis from DeepSeek AI."""
    headers = {
        "Content-Type": "application/json"
    }
    
    payload = {
    "model": "deepseek-chat",
    "messages": [
        {
            "role": "system",
            "content": "You are a Linux troubleshooting assistant. Analyze the given error message and provide only an accurate, executable fix. Do not assume issues unless explicitly mentioned in the error."
        },

        {
            "role": "user",
            "content": f"Error encountered: {error_message}. Suggest a correct fix with a fully executable command."
        }
    ],
    "temperature": 0.3
}

    try:
        response = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=30
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except requests.RequestException as e:
        return f"Error diagnosis failed: {str(e)}"


def main():
    """Main execution flow."""
    try:
        generate_ssh_key()
        
        if (vm_ip := get_existing_vm_ip()):
            logger.info(f"Found existing VM with IP: {vm_ip}")
        else:
            logger.info("No existing VM found, creating new instance")
            vm_ip = create_gcp_vm()
        
        wait_for_ssh(vm_ip)
        logger.info(f"VM ready at {vm_ip}")

        while True:
            try:
                user_input = input("\nAI Assistant: What task should I perform? (or 'exit' to quit)\nYou: ")
                if user_input.lower() in ['exit', 'quit']:
                    break
                
                if not user_input.strip():
                    continue
                
                command = clean_commands(interpret_command(user_input))
                print(f"\nGenerated Command: {command}")
                confirm = input("Execute this command? (y/n): ").lower()
                if confirm == 'y':
                    execute_command(vm_ip, command)
                else:
                    logger.info("Command execution cancelled")

            except KeyboardInterrupt:
                logger.info("\nOperation cancelled by user")
                break

    except Exception as e:
        logger.error(f"Critical error: {str(e)}")
    finally:
        logger.info("Cleaning up resources...")
        # Add VM cleanup logic here if desired

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)



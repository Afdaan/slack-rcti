import os
import sys
import logging
from flask import Flask, jsonify, request
from dotenv import load_dotenv
from handlers.ping_handler import ping_handler
from handlers.jenkins_handler import jenkins_handler
from utils.slack_utils import verify_slack_request

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('app.log')
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Configuration
ALLOWED_USERGROUPS = ['devops']
JENKINS_URL = os.getenv('JENKINS_URL')
JENKINS_USER = os.getenv('JENKINS_USER')
JENKINS_TOKEN = os.getenv('JENKINS_TOKEN')
SLACK_SIGNING_SECRET = os.getenv('SLACK_SIGNING_SECRET')
SLACK_BOT_TOKEN = os.getenv('SLACK_BOT_TOKEN')

# Validate environment variables
if not all([JENKINS_URL, JENKINS_USER, JENKINS_TOKEN, SLACK_SIGNING_SECRET]):
    logger.error("Missing required environment variables!")
    logger.error("Please set JENKINS_URL, JENKINS_USER, JENKINS_TOKEN, and SLACK_SIGNING_SECRET")
    sys.exit(1)

# Import Jenkins after environment validation to avoid unnecessary import errors
try:
    import jenkins
except ImportError as e:
    logger.error(f"Failed to import jenkins module: {e}")
    logger.error("Please ensure python-jenkins is installed: pip install python-jenkins")
    sys.exit(1)

# Initialize Jenkins connection
try:
    jenkins_server = jenkins.Jenkins(
        JENKINS_URL,
        username=JENKINS_USER,
        password=JENKINS_TOKEN
    )
except Exception as e:
    print(f"Warning: Failed to initialize Jenkins connection: {e}")
    jenkins_server = None

@app.route('/deploy', methods=['POST'])
def deploy():
    # Verify Slack request signature
    if not verify_slack_request(SLACK_SIGNING_SECRET):
        return jsonify({
            "response_type": "ephemeral",
            "text": "Invalid Slack request signature!"
        }), 401
    
    return jenkins_handler(jenkins_server, ALLOWED_USERGROUPS)

@app.route('/ping', methods=['POST', 'GET'])
def ping():
    if request.method == 'GET':
        return jsonify({
            "status": "ok",
            "message": "Server is running"
        })

    # For POST requests, verify Slack request signature
    if not verify_slack_request(SLACK_SIGNING_SECRET):
        return jsonify({
            "response_type": "ephemeral",
            "text": "Invalid Slack request signature!"
        }), 401

    return ping_handler()

if __name__ == '__main__':
    try:
        # Get port from environment variable or default to 3000
        port = int(os.getenv('PORT', 3000))
        
        # Run the app
        logger.info(f"Starting server on port {port}")
        app.run(host='0.0.0.0', port=port)
    except Exception as e:
        logger.error(f"Failed to start server: {e}")
        sys.exit(1)

import os
import sys
import logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv

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
ALLOWED_ROLES = ['devops']
JENKINS_URL = os.getenv('JENKINS_URL')
JENKINS_USER = os.getenv('JENKINS_USER')
JENKINS_TOKEN = os.getenv('JENKINS_TOKEN')
SLACK_TOKEN = os.getenv('SLACK_TOKEN')

# Validate environment variables
if not all([JENKINS_URL, JENKINS_USER, JENKINS_TOKEN, SLACK_TOKEN]):
    logger.error("Missing required environment variables!")
    logger.error("Please set JENKINS_URL, JENKINS_USER, JENKINS_TOKEN, and SLACK_TOKEN")
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

# Add connection check helper
def check_jenkins_connection():
    if not jenkins_server:
        return jsonify({
            "response_type": "ephemeral",
            "text": "❌ Jenkins connection is not available!"
        }), 503
    return None

def verify_slack_token(token):
    return token == SLACK_TOKEN

@app.route('/slash', methods=['POST'])
def trigger_jenkins_build():
    # Check Jenkins connection first
    connection_error = check_jenkins_connection()
    if connection_error:
        return connection_error

    # Verify Slack token
    if not verify_slack_token(request.form.get('token')):
        return jsonify({
            "response_type": "ephemeral",
            "text": "Invalid Slack token!"
        }), 401

    # Get Slack request parameters
    user = request.form.get('user_name')
    text = request.form.get('text', '').strip()
    
    # Check user permissions
    if user not in ALLOWED_ROLES:
        return jsonify({
            "response_type": "ephemeral",
            "text": f"❌ Sorry @{user}, lu gak punya akses buat trigger Jenkins!"
        })

    # Parse job name and parameters from text
    try:
        params = {}
        args = text.split()
        if not args:
            return jsonify({
                "response_type": "ephemeral",
                "text": "Usage: /jenkins-build <job_name> [param1=value1 param2=value2 ...]"
            })
        
        job_name = args[0]
        
        # Parse optional parameters
        for arg in args[1:]:
            if '=' in arg:
                key, value = arg.split('=', 1)
                params[key] = value
    
        # Trigger Jenkins build
        try:
            jenkins_server.build_job(job_name, parameters=params)
            return jsonify({
                "response_type": "in_channel",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"🚀 *Build Triggered!*\n• Job: `{job_name}`\n• Triggered by: @{user}"
                        }
                    }
                ]
            })
        except jenkins.JenkinsException as e:
            return jsonify({
                "response_type": "ephemeral",
                "text": f"❌ Jenkins Error: {str(e)}"
            })
            
    except Exception as e:
        return jsonify({
            "response_type": "ephemeral",
            "text": f"❌ Error: {str(e)}"
        })

@app.route('/ping', methods=['POST', 'GET'])
def ping():
    if request.method == 'GET':
        return jsonify({
            "status": "ok",
            "message": "Server is running"
        })

    # For POST requests, verify Slack token
    if not verify_slack_token(request.form.get('token')):
        return jsonify({
            "response_type": "ephemeral",
            "text": "Invalid Slack token!"
        }), 401

    user = request.form.get('user_name')
    return jsonify({
        "response_type": "in_channel",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🏓 Pong! Hai @{user}, server is up and running!"
                }
            }
        ]
    })

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

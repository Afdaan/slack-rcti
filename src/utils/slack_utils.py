import hmac
import hashlib
import time
from flask import request

def verify_slack_request(signing_secret):
    """Verify that the request is coming from Slack"""
    # Get timestamp and signature from headers
    timestamp = request.headers.get('X-Slack-Request-Timestamp', '')
    slack_signature = request.headers.get('X-Slack-Signature', '')
    
    if not timestamp or not slack_signature:
        return False
    
    # Check if the timestamp is too old (>5 minutes)
    if abs(time.time() - float(timestamp)) > 300:
        return False
    
    # Create the signature base string
    sig_basestring = f"v0:{timestamp}:{request.get_data().decode()}"
    
    # Calculate hash
    my_signature = 'v0=' + hmac.new(
        signing_secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    
    # Compare signatures
    return hmac.compare_digest(my_signature, slack_signature)

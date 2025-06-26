from flask import jsonify, request
import jenkins

def jenkins_handler(jenkins_server, allowed_roles):
    """Handle the /deploy slash command for Jenkins operations"""
    if not jenkins_server:
        return jsonify({
            "response_type": "ephemeral",
            "text": "❌ Jenkins connection is not available!"
        }), 503

    # Get Slack request parameters
    user = request.form.get('user_name')
    text = request.form.get('text', '').strip()
    
    # Check user permissions
    if user not in allowed_roles:
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

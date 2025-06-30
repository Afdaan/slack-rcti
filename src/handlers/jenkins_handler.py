from flask import jsonify, request
import jenkins
import re

def validate_branch_name(branch):
    """Validate branch name format"""
    return bool(re.match(r'^[a-zA-Z0-9_./\-]+$', branch))

def validate_commit_hash(commit):
    """Validate commit hash format"""
    return bool(re.match(r'^[a-f0-9]{5,40}$', commit.lower()))

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

    # Show help if no arguments
    if not text:
        return jsonify({
            "response_type": "ephemeral",
            "text": (
                "Usage: /deploy <name_service> branch=<branch_name> [commit=<commit_hash>]\n"
                "Example:\n"
                "  /deploy mobile-service branch=feature/login\n"
                "  /deploy backend-service branch=develop commit=abc123\n"
            )
        })

    try:
        # Parse command arguments
        params = {}
        args = text.split()
        if len(args) < 2:  # Need at least service name and branch
            return jsonify({
                "response_type": "ephemeral",
                "text": "❌ Error: Branch parameter is required!\n"
                        "Usage: /deploy <name_service> branch=<branch_name> [commit=<commit_hash>]"
            })

        # Get service name
        service_name = args[0]
        
        # Construct job names
        build_job = f"build-{service_name}"
        k8s_job = f"k8s-{service_name}"

        # Parse parameters
        branch_specified = False
        for arg in args[1:]:
            if '=' in arg:
                key, value = arg.split('=', 1)
                if key == 'branch':
                    if not validate_branch_name(value):
                        return jsonify({
                            "response_type": "ephemeral",
                            "text": f"❌ Invalid branch name format: {value}"
                        })
                    params['BRANCH'] = value
                    branch_specified = True
                elif key == 'commit':
                    if not validate_commit_hash(value):
                        return jsonify({
                            "response_type": "ephemeral",
                            "text": f"❌ Invalid commit hash format: {value}"
                        })
                    params['COMMIT'] = value

        # Check if branch was specified
        if not branch_specified:
            return jsonify({
                "response_type": "ephemeral",
                "text": "❌ Branch parameter is required!\n"
                        "Usage: /deploy <name_service> branch=<branch_name> [commit=<commit_hash>]"
            })

        # Verify build job exists
        try:
            job_info = jenkins_server.get_job_info(build_job)
        except jenkins.NotFoundException:
            return jsonify({
                "response_type": "ephemeral",
                "text": f"❌ Job not found: {build_job}"
            })

        # Trigger build job
        try:
            build_number = jenkins_server.build_job(build_job, parameters=params)
            build_url = f"{jenkins_server.get_job_url(build_job)}/{build_number}/console"

            # Prepare response message
            response_text = (
                f"🚀 *Deployment Started!*\n"
                f"• Service: `{service_name}`\n"
                f"• Branch: `{params['BRANCH']}`\n"
            )
            
            if 'COMMIT' in params:
                response_text += f"• Commit: `{params['COMMIT']}`\n"
            
            response_text += (
                f"• Build Job: {build_url}\n"
                f"• Triggered by: @{user}\n\n"
                f"_Note: `{k8s_job}` will be triggered automatically after build completes_"
            )

            return jsonify({
                "response_type": "in_channel",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": response_text
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

from flask import jsonify, request
import jenkins
import re
import os
import logging
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

def validate_branch_name(branch):
    """Validate branch name format"""
    return bool(re.match(r'^[a-zA-Z0-9_./\-]+$', branch))

def validate_commit_hash(commit):
    """Validate commit hash format"""
    return bool(re.match(r'^[a-f0-9]{5,40}$', commit.lower()))

def check_user_in_usergroup(user_id, usergroup):
    """Check if user is member of specified Slack User Group"""
    try:
        # Initialize Slack client
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
        
        client = WebClient(token=os.getenv('SLACK_BOT_TOKEN'))
        
        # Get user groups
        response = client.usergroups_list()
        if not response["ok"]:
            logger.error(f"Failed to get user groups: {response['error']}")
            return False
            
        # Find the requested usergroup
        usergroup_id = None
        for group in response["usergroups"]:
            if group["handle"] == usergroup:
                usergroup_id = group["id"]
                break
                
        if not usergroup_id:
            logger.error(f"Usergroup not found: {usergroup}")
            return False
            
        # Get users in the group
        response = client.usergroups_users_list(usergroup=usergroup_id)
        if not response["ok"]:
            logger.error(f"Failed to get usergroup members: {response['error']}")
            return False
            
        return user_id in response["users"]
        
    except Exception as e:
        logger.error(f"Error checking user group membership: {e}")
        return False

def jenkins_handler(jenkins_server, allowed_usergroups):
    """Handle the /deploy slash command for Jenkins operations"""
    if not jenkins_server:
        return jsonify({
            "response_type": "ephemeral",
            "text": "❌ Jenkins connection is not available!"
        }), 503

    # Get Slack request parameters
    user_id = request.form.get('user_id')
    user_name = request.form.get('user_name')
    text = request.form.get('text', '').strip()
    
    # TODO: Uncomment this block later for proper user group checking
    # has_access = any(check_user_in_usergroup(user_id, group) for group in allowed_usergroups)
    
    # if not has_access:
    #     return jsonify({
    #         "response_type": "ephemeral",
    #         "text": f"❌ Sorry @{user_name}, lu gak punya akses buat trigger Jenkins!\nPerlu join Slack User Group: {', '.join(allowed_usergroups)}"
    #     })
    
    # Temporary: Allow all users (DEVELOPMENT ONLY)
    has_access = True

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

        # Verify both build and k8s jobs exist
        try:
            build_info = jenkins_server.get_job_info(build_job)
            k8s_info = jenkins_server.get_job_info(k8s_job)
        except jenkins.NotFoundException as e:
            missing_job = build_job if "build" in str(e) else k8s_job
            return jsonify({
                "response_type": "ephemeral",
                "text": f"❌ Job not found: {missing_job}"
            })

        # Trigger build job
        try:
            # Set same parameters for k8s job
            k8s_params = {
                'BRANCH': params['BRANCH']  # Pass the same branch to k8s job
            }
            
            # Start the build job
            build_number = jenkins_server.build_job(build_job, parameters=params)
            build_url = f"{jenkins_server.get_job_url(build_job)}/{build_number}/console"
            k8s_url = jenkins_server.get_job_url(k8s_job)

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
                f"• K8s Job: {k8s_url}\n"
                f"• Triggered by: @{user_name}\n\n"
                f"_Note: `{k8s_job}` will be triggered automatically with branch=`{params['BRANCH']}`_"
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

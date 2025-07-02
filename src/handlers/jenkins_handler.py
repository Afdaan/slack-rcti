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
    # Clean the branch name first (remove any leading/trailing spaces)
    branch = branch.strip()
    
    # Basic validation: allow letters, numbers, and common branch characters
    is_valid = bool(re.match(r'^[a-zA-Z0-9_./\-]+$', branch))
    
    if not is_valid:
        logger.warning(f"Branch validation failed. Branch: '{branch}', Length: {len(branch)}, Chars: {[ord(c) for c in branch]}")
    
    return is_valid

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
                "Usage: /deploy <job_name> branch=<branch_name> [commit=<commit_hash>]\n"
                "Example:\n"
                "  /deploy my-service branch=feature/login\n"
                "  /deploy backend-service branch=develop commit=abc123\n"
            )
        })

    try:
        # Parse command arguments
        params = {}
        args = text.split()
        if len(args) < 2:  # Need at least job name and branch
            return jsonify({
                "response_type": "ephemeral",
                "text": "❌ Error: Branch parameter is required!\n"
                        "Usage: /deploy <job_name> branch=<branch_name> [commit=<commit_hash>]"
            })

        # Get job name exactly as provided
        job_name = args[0].strip()
        logger.info(f"Processing job: {job_name}")

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
                        "Usage: /deploy <job_name> branch=<branch_name> [commit=<commit_hash>]"
            })

        # Verify job exists
        try:
            job_info = jenkins_server.get_job_info(job_name)
        except jenkins.NotFoundException:
            return jsonify({
                "response_type": "ephemeral",
                "text": f"❌ Job not found: {job_name}"
            })

        # Start building response message
        response_text = (
            f"🚀 *Deployment Started!*\n"
            f"• Job: `{job_name}`\n"
            f"• Branch: `{params['BRANCH']}`\n"
        )
        
        if 'COMMIT' in params:
            response_text += f"• Commit: `{params['COMMIT']}`\n"

        # Get downstream jobs that will be triggered after this one
        downstream_jobs = []
        if 'downstreamProjects' in job_info:
            for downstream in job_info['downstreamProjects']:
                downstream_name = downstream.get('name')
                if downstream_name:
                    try:
                        # Verify downstream job exists and add to list
                        jenkins_server.get_job_info(downstream_name)
                        downstream_jobs.append(downstream_name)
                    except jenkins.NotFoundException:
                        logger.warning(f"Downstream job not found: {downstream_name}")
                        continue

        # Trigger the job
        try:
            build_number = jenkins_server.build_job(job_name, parameters=params)
            job_url = f"{jenkins_server.get_job_url(job_name)}/{build_number}/console"
            response_text += f"• Job URL: {job_url}\n"
            
            # Add information about downstream jobs
            if downstream_jobs:
                response_text += f"• Downstream Jobs: {', '.join(f'`{j}`' for j in downstream_jobs)}\n"
                response_text += f"_Note: Downstream jobs will be triggered automatically with branch=`{params['BRANCH']}`_\n"
            
            response_text += f"• Triggered by: @{user_name}"

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

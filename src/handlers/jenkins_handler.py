from flask import jsonify, request
import re
import os
import logging
from jenkinsapi.jenkins import Jenkins
from jenkinsapi.custom_exceptions import JenkinsAPIException
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
        
    # Use the existing Jenkins connection
    jenkins = jenkins_server
    logger.info("Using existing Jenkins connection")

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
        logger.info(f"Processing job request: {job_name}")

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

        # Verify job exists and get job info
        try:
            logger.info(f"Checking if job exists: {job_name}")
            
            try:
                # Check if job exists directly
                if job_name not in jenkins:
                    logger.error(f"Job {job_name} not found in Jenkins")
                    return jsonify({
                        "response_type": "ephemeral",
                        "text": f"❌ Job not found: {job_name}"
                    })
                
                # Get the job
                job = jenkins[job_name]
                logger.info(f"Found job: {job_name}")
                
            except Exception as e:
                logger.error(f"Error checking job: {str(e)}")
                return jsonify({
                    "response_type": "ephemeral",
                    "text": f"❌ Error accessing Jenkins: {str(e)}"
                })
            
            # Get downstream jobs
            downstream_jobs = []
            try:
                downstream_info = job.get_downstream_jobs()
                for downstream in downstream_info:
                    downstream_jobs.append(downstream.name)
                    logger.info(f"Found downstream job: {downstream.name}")
            except Exception as e:
                logger.warning(f"Error getting downstream jobs: {str(e)}")
            
            # Trigger build with parameters
            logger.info(f"Triggering build for {job_name} with params: {params}")
            queue_item = job.invoke(build_params=params)
            logger.info("Build queued successfully")
            
            # Wait for build number (with timeout)
            try:
                build = queue_item.get_build(timeout=10)
                build_number = build.buildno
                build_url = build.baseurl
                logger.info(f"Build started: #{build_number}")
            except Exception as e:
                logger.warning(f"Could not get build number/URL: {str(e)}")
                build_number = "queued"
                build_url = job.baseurl
            
            # Build response message
            response_text = (
                f"🚀 *Deployment Started!*\n"
                f"• Job: `{job_name}`\n"
                f"• Branch: `{params['BRANCH']}`\n"
            )
            
            if 'COMMIT' in params:
                response_text += f"• Commit: `{params['COMMIT']}`\n"
                
            response_text += f"• Build: #{build_number}\n"
            response_text += f"• URL: {build_url}\n"
            
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

        except JenkinsAPIException as e:
            logger.error(f"Jenkins API error: {str(e)}")
            if "401" in str(e):
                error_msg = "❌ Authentication failed. Please check Jenkins credentials."
            elif "403" in str(e):
                error_msg = "❌ Access denied. Please check your permissions."
            else:
                error_msg = f"❌ Jenkins Error: {str(e)}"
            
            return jsonify({
                "response_type": "ephemeral",
                "text": error_msg
            })

    except Exception as e:
        logger.error(f"Unexpected error in jenkins_handler: {str(e)}")
        return jsonify({
            "response_type": "ephemeral",
            "text": f"❌ Error: {str(e)}"
        })

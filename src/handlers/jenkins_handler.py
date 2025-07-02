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
        logger.warning(f"Invalid branch name format: {branch}")
    
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
        branch_value = None  # Store branch value for downstream jobs
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
                    branch_value = value.strip()  # Store branch value
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

                # Check if job accepts BRANCH parameter
                job_config = job.get_config()
                has_branch_param = 'BRANCH' in job_config
                if has_branch_param:
                    params['BRANCH'] = branch_value
                
            except Exception as e:
                logger.error(f"Error checking job: {str(e)}")
                return jsonify({
                    "response_type": "ephemeral",
                    "text": f"❌ Error accessing Jenkins: {str(e)}"
                })
            
            # Get downstream jobs
            downstream_jobs = []
            # Get downstream jobs and handle them first
            downstream_jobs = []
            try:
                downstream_info = job.get_downstream_jobs()
                for downstream in downstream_info:
                    downstream_jobs.append(downstream.name)
                    logger.info(f"Found downstream job: {downstream.name}")
                    
                    # Try to get downstream job and check if it accepts BRANCH parameter
                    try:
                        downstream_job = jenkins[downstream.name]
                        downstream_config = downstream_job.get_config()
                        if 'BRANCH' in downstream_config:
                            logger.info(f"Downstream job {downstream.name} accepts BRANCH parameter")
                    except Exception as e:
                        logger.warning(f"Could not check downstream job {downstream.name} parameters: {str(e)}")
                        
            except Exception as e:
                logger.warning(f"Error getting downstream jobs: {str(e)}")
            
            # Trigger build with parameters (if any)
            logger.info(f"Triggering build for {job_name}" + (f" with params: {params}" if params else " without params"))
            try:
                if params:
                    queue_item = job.invoke(build_params=params)
                else:
                    queue_item = job.invoke()
                logger.info("Build queued successfully")
                
                # Wait for build number
                try:
                    build = queue_item.get_build()
                    build_number = build.buildno
                    build_url = build.baseurl
                    logger.info(f"Build started: #{build_number}")
                    
                    # Now that we have the main build, try to trigger downstream jobs with branch
                    for downstream_name in downstream_jobs:
                        try:
                            downstream_job = jenkins[downstream_name]
                            downstream_config = downstream_job.get_config()
                            if 'BRANCH' in downstream_config and branch_value:
                                downstream_job.invoke(build_params={'BRANCH': branch_value})
                                logger.info(f"Triggered downstream job {downstream_name} with BRANCH={branch_value}")
                        except Exception as e:
                            logger.warning(f"Could not trigger downstream job {downstream_name}: {str(e)}")
                            
                except Exception as e:
                    logger.warning(f"Could not get build number/URL: {str(e)}")
                    build_number = "queued"
                    build_url = job.baseurl
                    
            except Exception as e:
                if "This job does not support parameters" in str(e):
                    # Try without parameters
                    queue_item = job.invoke()
                    logger.info("Build queued successfully without parameters")
                    
                    try:
                        build = queue_item.get_build()
                        build_number = build.buildno
                        build_url = build.baseurl
                        logger.info(f"Build started: #{build_number}")
                    except Exception as e:
                        logger.warning(f"Could not get build number/URL: {str(e)}")
                        build_number = "queued"
                        build_url = job.baseurl
                else:
                    raise e
            
            # Build response message
            response_text = (
                f"🚀 *Deployment Started!*\n"
                f"• Job: `{job_name}`\n"
            )
            
            if 'BRANCH' in params:
                response_text += f"• Branch: `{params['BRANCH']}`\n"
            elif branch_value:  # Fallback to branch_value if not in params
                response_text += f"• Branch: `{branch_value}`\n"
            
            if 'COMMIT' in params:
                response_text += f"• Commit: `{params['COMMIT']}`\n"
                
            response_text += f"• Build: <{build_url}|#{build_number}>\n"
            
            if downstream_jobs:
                response_text += f"• Downstream Jobs: {', '.join(f'`{j}`' for j in downstream_jobs)}\n"
                if branch_value:
                    response_text += f"_Note: Downstream jobs will be triggered with branch=`{branch_value}`_\n"
            
            response_text += f"• Triggered by: @{user_name}"

            return jsonify({
                "response_type": "in_channel",
                "text": response_text
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
    try:
        # Parse job name and parameters from the command
        command_parts = data.get('text', '').strip().split()
        if not command_parts:
            return jsonify({
                "response_type": "ephemeral",
                "text": "❌ Error: Please provide a job name"
            })
        
        job_name = command_parts[0]
        params = {}
        branch_value = None  # Store branch value separately for downstream jobs
        
        # Parse parameters (format: key=value)
        for part in command_parts[1:]:
            if '=' in part:
                key, value = part.split('=', 1)
                params[key.upper()] = value
                if key.upper() == 'BRANCH':
                    branch_value = value
        
        logger.info(f"Deploying job: {job_name} with params: {params}")
        
        try:
            job = jenkins[job_name]
        except Exception as e:
            logger.error(f"Job not found: {job_name}")
            return jsonify({
                "response_type": "ephemeral",
                "text": f"❌ Error: Job `{job_name}` not found"
            })
        
        # Get downstream jobs
        downstream_jobs = []
        try:
            downstream_info = job.get_downstream_jobs()
            for downstream in downstream_info:
                downstream_jobs.append(downstream.name)
                logger.info(f"Found downstream job: {downstream.name}")
                
                # Try to get downstream job and check if it accepts BRANCH parameter
                try:
                    downstream_job = jenkins[downstream.name]
                    downstream_config = downstream_job.get_config()
                    if 'BRANCH' in downstream_config:
                        # Don't modify the config, but note that this job needs the branch
                        logger.info(f"Downstream job {downstream.name} accepts BRANCH parameter")
                        # Try to build with parameters directly
                        try:
                            downstream_job.invoke(build_params={'BRANCH': branch_value})
                            logger.info(f"Triggered downstream job {downstream.name} with BRANCH={branch_value}")
                        except Exception as e:
                            logger.warning(f"Could not trigger downstream job {downstream.name} with parameters: {str(e)}")
                except Exception as e:
                    logger.warning(f"Could not check downstream job {downstream.name} parameters: {str(e)}")
                    
        except Exception as e:
            logger.warning(f"Error getting downstream jobs: {str(e)}")
            # Continue with the main job even if downstream job check fails
        
        # Trigger build with parameters (if any)
        logger.info(f"Triggering build for {job_name}" + (f" with params: {params}" if params else " without params"))
        try:
            if params:
                queue_item = job.invoke(build_params=params)
            else:
                queue_item = job.invoke()
            logger.info("Build queued successfully")
        except Exception as e:
            if "This job does not support parameters" in str(e):
                # Try without parameters
                queue_item = job.invoke()
                logger.info("Build queued successfully without parameters")
            else:
                raise e
        
        # Wait for build number (without timeout)
        try:
            build = queue_item.get_build()  # No timeout parameter
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
        )
        
        if 'BRANCH' in params:
            response_text += f"• Branch: `{params['BRANCH']}`\n"
        elif branch_value:  # Fallback to branch_value if not in params
            response_text += f"• Branch: `{branch_value}`\n"
            
        if downstream_jobs:
            response_text += f"• Downstream jobs: `{', '.join(downstream_jobs)}`\n"
        
        response_text += f"• Build: <{build_url}|#{build_number}>"
        
        return jsonify({
            "response_type": "in_channel",
            "text": response_text
        })
        
    except JenkinsAPIException as e:
        logger.error(f"Jenkins API error: {str(e)}")
        return jsonify({
            "response_type": "ephemeral",
            "text": f"❌ Jenkins API Error: {str(e)}"
        })
        
    except Exception as e:
        logger.error(f"Unexpected error in jenkins_handler: {str(e)}")
        return jsonify({
            "response_type": "ephemeral",
            "text": f"❌ Error: {str(e)}"
        })

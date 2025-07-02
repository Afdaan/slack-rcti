from flask import jsonify, request
import re
import os
import logging
import time
import threading
from jenkinsapi.jenkins import Jenkins
from jenkinsapi.custom_exceptions import JenkinsAPIException
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

def update_slack_message(client, channel, ts, text):
    """Update a Slack message"""
    try:
        client.chat_update(
            channel=channel,
            ts=ts,
            text=text
        )
    except SlackApiError as e:
        logger.error(f"Error updating Slack message: {e}")

def wait_for_build_to_start(queue_item, max_attempts=20, delay=2):
    """Wait for a queued build to start and return build info"""
    for attempt in range(max_attempts):
        try:
            build = queue_item.get_build()
            if build:
                return build
        except Exception as e:
            logger.debug(f"Build not ready yet (attempt {attempt + 1}/{max_attempts}): {str(e)}")
        time.sleep(delay)
    return None

def wait_for_build_success(build, max_attempts=30, delay=10):
    """Wait for a build to complete successfully"""
    for attempt in range(max_attempts):
        try:
            build.poll()  # Refresh build info
            if build.is_running():
                logger.debug(f"Build {build.buildno} still running...")
            else:
                status = build.get_status()
                logger.info(f"Build {build.buildno} finished with status: {status}")
                return status == "SUCCESS"
        except Exception as e:
            logger.debug(f"Error polling build (attempt {attempt + 1}/{max_attempts}): {str(e)}")
        time.sleep(delay)
    return False

def handle_downstream_build(jenkins, main_build, downstream_name, branch_value, slack_client=None, channel=None, thread_ts=None):
    """Handle downstream build that is auto-triggered by Jenkins"""
    try:
        # First wait for the main build to complete successfully
        if not wait_for_build_success(main_build):
            logger.error(f"Main build failed or timed out, downstream job {downstream_name} may not trigger")
            if slack_client and channel and thread_ts:
                slack_client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"❌ Main build failed or timed out, downstream job {downstream_name} may not trigger"
                )
            return None

        logger.info(f"Main build successful, monitoring for auto-triggered {downstream_name}")
        if slack_client and channel and thread_ts:
            slack_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"✅ Main build successful, monitoring for auto-triggered {downstream_name}"
            )

        # Get the downstream job
        downstream_job = jenkins[downstream_name]
        
        # Wait for the auto-triggered build to appear (it might take a few seconds)
        max_attempts = 10
        build = None
        for attempt in range(max_attempts):
            try:
                # Get the latest build
                latest_build = downstream_job.get_last_build()
                if latest_build:
                    # Check the build cause
                    cause = get_build_cause(latest_build)
                    if (cause and 
                        cause['type'] == 'upstream' and 
                        cause['project'] == main_build.job.name and
                        cause['build'] == main_build.buildno):
                        build = latest_build
                        break
                    # Fallback to timestamp check if cause check fails
                    elif latest_build.get_timestamp() > main_build.get_timestamp():
                        build = latest_build
                        break
            except Exception as e:
                logger.debug(f"Error checking for new build (attempt {attempt + 1}): {str(e)}")
            time.sleep(5)  # Wait before next check

        if build:
            status_msg = f"Found auto-triggered downstream job {downstream_name}: <{build.baseurl}|#{build.buildno}>"
            if slack_client and channel and thread_ts:
                slack_client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=status_msg
                )
            logger.info(status_msg)
            
            # Monitor for input state with increased checks
            max_input_checks = 15  # More attempts
            for attempt in range(max_input_checks):
                try:
                    build.poll()  # Refresh build info
                    build_url = build.baseurl
                    
                    # Check if build is waiting for input
                    if '/input/' in build_url:
                        logger.info(f"Found input prompt in {downstream_name} (attempt {attempt + 1})")
                        try:
                            # Submit the branch parameter
                            input_url = f"{build_url}input/proceed"
                            jenkins.requester.post_and_confirm_status(
                                input_url,
                                data={
                                    'json': f'{{"parameter": {{"name": "BRANCH", "value": "{branch_value}"}}}}',
                                    'proceed': 'true'
                                }
                            )
                            status_msg = f"✅ Submitted branch parameter to {downstream_name}"
                            if slack_client and channel and thread_ts:
                                slack_client.chat_postMessage(
                                    channel=channel,
                                    thread_ts=thread_ts,
                                    text=status_msg
                                )
                            logger.info(status_msg)
                            return build
                        except Exception as e:
                            logger.warning(f"Failed to submit input parameter: {str(e)}")
                except Exception as e:
                    logger.debug(f"Error checking build status (attempt {attempt + 1}): {str(e)}")
                time.sleep(4)  # Longer delay between checks
        else:
            logger.warning(f"No new build detected for {downstream_name} after {max_attempts} attempts")
            if slack_client and channel and thread_ts:
                slack_client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"⚠️ No new build detected for {downstream_name} after monitoring for {max_attempts * 5} seconds"
                )
        
        return build
    except Exception as e:
        logger.error(f"Error handling downstream build {downstream_name}: {str(e)}")
        return None

def monitor_downstream_build(jenkins, downstream_name, branch_value, main_build_time, max_attempts=12):
    """Monitor for a new downstream build after main build"""
    for attempt in range(max_attempts):
        try:
            downstream_job = jenkins[downstream_name]
            build = downstream_job.get_last_build()
            if build and build.get_timestamp() > main_build_time:
                return build
        except Exception as e:
            logger.debug(f"Error checking downstream build (attempt {attempt + 1}): {str(e)}")
        time.sleep(5)  # Wait 5 seconds between checks
    return None

def async_handle_builds(job_name, build, downstream_jobs, branch_value, channel_id, thread_ts):
    """Asynchronously handle main build and monitor downstream builds"""
    try:
        slack_client = WebClient(token=os.getenv('SLACK_BOT_TOKEN'))
        
        # Monitor main build for input and completion
        if check_and_handle_input(build, branch_value):
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"✅ Submitted branch parameter to {job_name}"
            )
        
        # Wait for main build to complete
        if not wait_for_build_success(build):
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"❌ Main build failed or timed out, downstream jobs may not trigger"
            )
            return

        # Get the main build completion time
        main_build_time = build.get_timestamp()
        
        # Monitor downstream builds that should be auto-triggered
        for downstream_name in downstream_jobs:
            # Wait and look for new downstream build
            downstream_build = monitor_downstream_build(
                build.job.jenkins,
                downstream_name,
                branch_value,
                main_build_time
            )
            
            if downstream_build:
                status_msg = f"Downstream job {downstream_name} started: <{downstream_build.baseurl}|#{downstream_build.buildno}>"
                slack_client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=status_msg
                )
                
                # Monitor for input parameter needs
                max_input_checks = 15
                for _ in range(max_input_checks):
                    if check_and_handle_input(downstream_build, branch_value):
                        slack_client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=thread_ts,
                            text=f"✅ Submitted branch parameter to {downstream_name}"
                        )
                        break
                    time.sleep(4)
            else:
                slack_client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=f"⚠️ Could not detect downstream job {downstream_name}, please check manually"
                )
    except Exception as e:
        logger.error(f"Error in async build handling: {str(e)}")

def check_and_handle_input(build, branch_value):
    """Check if build is waiting for input and handle Pipeline Input Step"""
    try:
        if not hasattr(build, 'is_running') or not build.is_running():
            return False

        # Get the build info to check for input action
        build_url = build.baseurl
        build.poll()  # Refresh build info
        
        # Check if build has InputAction in its actions
        build_info = build.get_actions()
        has_input = any(
            action.get('_class') == 'org.jenkinsci.plugins.workflow.support.steps.input.InputAction'
            for action in build_info
        )
        
        if has_input and '/input/' in build_url:
            logger.info(f"Build {build.buildno} is waiting for input")
            try:
                # For Pipeline Input Step, we need to submit the input differently
                input_url = f"{build_url}input/submit"
                
                # First, get the input form to check what parameters it needs
                form_data = build.job.jenkins.requester.get_url(f"{build_url}input/").text
                
                # Prepare the input submission data
                submit_data = {
                    'proceed': 'true',
                    'Jenkins-Crumb': build.job.jenkins.requester.CRUMB,
                }
                
                # If the form contains BRANCH parameter, add it
                if 'BRANCH' in form_data:
                    submit_data.update({
                        'BRANCH': branch_value,
                        'json': f'{{"parameter": {{"name": "BRANCH", "value": "{branch_value}"}}}}'
                    })
                
                # Submit the input
                response = build.job.jenkins.requester.post_and_confirm_status(
                    input_url,
                    data=submit_data
                )
                
                if response.status_code == 200:
                    logger.info(f"Successfully submitted input for build {build.buildno}")
                    return True
                else:
                    logger.warning(f"Input submission returned status {response.status_code}")
                    return False
                    
            except Exception as e:
                logger.warning(f"Could not submit input: {str(e)}")
                return False
    except Exception as e:
        logger.warning(f"Error checking build status: {str(e)}")
        return False
    return False

def validate_branch_name(branch):
    """Validate branch name format"""
    # Clean the branch name first (remove any leading/trailing spaces)
    branch = branch.strip()
    
    # Basic validation: allow letters, numbers, and common branch characters
    is_valid = bool(re.match(r'^[a-zA-Z0-9_./\-]+$', branch))
    
    if not is_valid:
        logger.warning(f"Branch validation failed: {branch}")
    
    return is_valid

def validate_commit_hash(commit):
    """Validate commit hash format"""
    return bool(re.match(r'^[a-f0-9]{5,40}$', commit.lower()))

def check_user_in_usergroup(user_id, usergroup):
    """Check if user is member of specified Slack User Group"""
    try:
        # Initialize Slack client
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
                    params['BRANCH'] = value  # Always add branch to params
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

        try:
            logger.info(f"Checking if job exists: {job_name}")
            
            if job_name not in jenkins:
                logger.error(f"Job {job_name} not found in Jenkins")
                return jsonify({
                    "response_type": "ephemeral",
                    "text": f"❌ Job not found: {job_name}"
                })
            
            # Get the job
            job = jenkins[job_name]
            logger.info(f"Found job: {job_name}")

            # Get downstream jobs first
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
            try:
                queue_item = job.invoke(build_params=params)
                logger.info("Build queued successfully")
            except Exception as e:
                if "This job does not support parameters" in str(e):
                    queue_item = job.invoke()
                    logger.info("Build queued successfully without parameters")
                else:
                    raise e
            
            # Wait for build to start with initial polling
            build = wait_for_build_to_start(queue_item, max_attempts=5)  # Quick initial check
            if build:
                build_number = build.buildno
                build_url = build.baseurl
                logger.info(f"Build started: #{build_number}")
            else:
                logger.warning("Build not started yet, will continue monitoring in background")
                build_number = "queued"
                build_url = job.baseurl

            # Get channel ID from request for async updates
            channel_id = request.form.get('channel_id')
            
            # Prepare initial response
            response_text = (
                f"🚀 *Deployment Started!*\n"
                f"• Job: `{job_name}`\n"
                f"• Branch: `{branch_value}`\n"
            )
            
            if 'COMMIT' in params:
                response_text += f"• Commit: `{params['COMMIT']}`\n"
            
            response_text += f"• Build: <{build_url}|#{build_number}>\n"
            
            if downstream_jobs:
                response_text += f"• Downstream Jobs: {', '.join(f'`{j}`' for j in downstream_jobs)}\n"
                response_text += f"_Note: Downstream jobs will be triggered with branch=`{branch_value}`_\n"
            
            response_text += f"• Triggered by: @{user_name}"

            # Send initial response
            response = jsonify({
                "response_type": "in_channel",
                "text": response_text
            })

            # Start async monitoring in a separate thread
            if build:
                thread = threading.Thread(
                    target=async_handle_builds,
                    args=(job_name, build, downstream_jobs, branch_value, channel_id, response.headers.get('X-Slack-Message-Ts'))
                )
                thread.start()
                
            return response
            
            # Build response message
            response_text = (
                f"🚀 *Deployment Started!*\n"
                f"• Job: `{job_name}`\n"
                f"• Branch: `{branch_value}`\n"
            )
            
            if 'COMMIT' in params:
                response_text += f"• Commit: `{params['COMMIT']}`\n"
            
            response_text += f"• Build: <{build_url}|#{build_number}>\n"
            
            if downstream_jobs:
                response_text += f"• Downstream Jobs: {', '.join(f'`{j}`' for j in downstream_jobs)}\n"
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

def get_build_cause(build):
    """Get the cause of a build, especially useful for checking upstream triggers"""
    try:
        actions = build.get_actions()
        for action in actions:
            if action.get('_class') == 'hudson.model.CauseAction':
                causes = action.get('causes', [])
                for cause in causes:
                    if cause.get('_class') == 'hudson.model.Cause$UpstreamCause':
                        return {
                            'type': 'upstream',
                            'project': cause.get('upstreamProject'),
                            'build': cause.get('upstreamBuild'),
                            'url': cause.get('upstreamUrl')
                        }
        return None
    except Exception as e:
        logger.debug(f"Error getting build cause: {str(e)}")
        return None

def is_waiting_for_input(build):
    """Check if a build is waiting for input"""
    try:
        actions = build.get_actions()
        return any(
            action.get('_class') == 'org.jenkinsci.plugins.workflow.support.steps.input.InputAction'
            for action in actions
        )
    except Exception as e:
        logger.debug(f"Error checking input status: {str(e)}")
        return False

from flask import jsonify, request

def ping_handler():
    """Handle the /ping slash command"""
    if request.method == 'GET':
        return jsonify({
            "status": "ok",
            "message": "Server is running"
        })

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

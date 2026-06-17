//python contents
import os
import ssl
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


def post_as_user(channel, message):
    user_token =  "xoxp-10657894445121-3828541219168-10662304630532-f66b4cbbdaff1339f9a7a273eab8fbcd"

    # Create an unverified SSL context to bypass the error
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    # Pass the unverified context to the WebClient
    client = WebClient(token=user_token, ssl=ssl_context)

    try:
        response = client.chat_postMessage(
            channel=channel,
            text=message
        )
        print(f"Success! TS: {response['ts']}")
    except SlackApiError as e:
        print(f"Slack API Error: {e.response['unicorn']}")

"""Cognito Post-Confirmation Lambda trigger — first-admin bootstrap.

Runs immediately after a user confirms their email. Checks whether the
`bedrock-lens-admins` group has any members; if not, adds the just-confirmed
user. After that initial promotion it's a no-op.

This avoids the chicken-and-egg of "first deploy has no admin and no way
to make one without dropping into the AWS console". The first person to
verify their email becomes the dashboard admin.

Side effects: lists users in the admins group (1 ListUsersInGroup call),
optionally adds the user (1 AdminAddUserToGroup call). Both are scoped to
this stack's User Pool by IAM.

Env vars:
    ADMIN_GROUP   Group name to bootstrap into (default: bedrock-lens-admins)
"""
import os

import boto3


ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "bedrock-lens-admins")
_client = boto3.client("cognito-idp")


def handler(event, context):  # noqa: ARG001
    # Bail out unless this is the post-confirmation of an actual sign-up.
    # Cognito reuses this trigger for confirm-forgot-password too — we
    # don't want to re-promote on every password reset.
    if event.get("triggerSource") != "PostConfirmation_ConfirmSignUp":
        return event

    user_pool_id = event.get("userPoolName") or event.get("userPoolId")
    user_name = event.get("userName")
    if not user_pool_id or not user_name:
        # Defensive — shouldn't happen, but never fail-close on a confirm.
        return event

    try:
        existing = _client.list_users_in_group(
            UserPoolId=user_pool_id,
            GroupName=ADMIN_GROUP,
            Limit=1,
        )
    except _client.exceptions.ResourceNotFoundException:
        # Group not provisioned yet — nothing we can do; carry on.
        return event

    if existing.get("Users"):
        # Already at least one admin; this user signs up as a regular user.
        return event

    try:
        _client.admin_add_user_to_group(
            UserPoolId=user_pool_id,
            Username=user_name,
            GroupName=ADMIN_GROUP,
        )
        print(f"Auto-promoted first user '{user_name}' to {ADMIN_GROUP}")
    except Exception as e:
        # Logging only; the sign-up itself already succeeded. Operator can
        # add the admin manually via `aws cognito-idp admin-add-user-to-group`.
        print(f"WARNING: failed to auto-promote {user_name} to {ADMIN_GROUP}: {e}")

    return event

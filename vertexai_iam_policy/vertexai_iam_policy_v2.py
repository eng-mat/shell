#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
import os

# --- Configuration (Bundled Roles) ---
BUNDLED_ROLES_CONFIG = {
    "GenAI_ADMIN": [
        "roles/aiplatform.admin",
        "roles/aiplatform.user",
        "roles/notebooks.admin",
        "roles/storage.admin",
        "roles/bigquery.admin",
    ],
    "GenAI_DEVELOPER": [
        "roles/aiplatform.user",
        "roles/viewer",
        "roles/bigquery.dataViewer",
        "roles/storage.objectViewer",
    ],
    "CUSTOM_BUNDLE_1": [
        "roles/compute.admin",
        "roles/container.admin"
    ],
}

def run_gcloud_command(command_args, expect_json=True, attempt_login=False):
    """
    Runs a gcloud command and returns the output.
    If expect_json is True, parses stdout as JSON.
    Handles errors and prints stderr.
    """
    try:
        # Ensure CLOUDSDK_CORE_PROJECT is set if project_id is part of command_args
        # This helps gcloud pick up the project correctly, especially in automated environments.
        env = os.environ.copy()
        project_id_index = -1
        try:
            project_id_index = command_args.index("--project") # for gcloud projects set-iam-policy
        except ValueError:
            try:
                # for gcloud projects get-iam-policy project_id
                if command_args[0] == "projects" and command_args[1] == "get-iam-policy" and len(command_args) > 2:
                    # Heuristic: if the third element doesn't start with '--', it's likely the project ID
                    if not command_args[2].startswith("--"):
                         env["CLOUDSDK_CORE_PROJECT"] = command_args[2]
            except IndexError:
                pass # Not a get-iam-policy command or not enough args

        process = subprocess.run(
            ["gcloud"] + command_args,
            capture_output=True,
            text=True,
            check=False, # We will check the returncode manually
            env=env
        )

        if process.returncode != 0:
            print(f"Error: gcloud command failed: {' '.join(['gcloud'] + command_args)}", file=sys.stderr)
            print(f"gcloud stderr:\n{process.stderr}", file=sys.stderr)
            if attempt_login and " douleur" in process.stderr.lower(): # Example error suggesting auth issue
                 print("Authentication error detected. Please ensure you are logged in to gcloud and have the correct permissions.", file=sys.stderr)
            sys.exit(process.returncode)

        if expect_json:
            if not process.stdout.strip():
                print(f"Error: gcloud command returned empty stdout, expected JSON: {' '.join(['gcloud'] + command_args)}", file=sys.stderr)
                print(f"gcloud stderr:\n{process.stderr}", file=sys.stderr) # Print stderr for context
                sys.exit(1)
            try:
                return json.loads(process.stdout)
            except json.JSONDecodeError as e:
                print(f"Error: Failed to decode JSON from gcloud output: {e}", file=sys.stderr)
                print(f"gcloud stdout:\n{process.stdout}", file=sys.stderr)
                sys.exit(1)
        return process.stdout.strip()

    except FileNotFoundError:
        print("Error: gcloud CLI not found. Please ensure it's installed and in your PATH.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred while running gcloud command: {e}", file=sys.stderr)
        sys.exit(1)


def get_bundled_roles(bundle_name):
    """Resolves a bundled role name to a list of actual IAM roles."""
    if not bundle_name:
        return []
    roles = BUNDLED_ROLES_CONFIG.get(bundle_name)
    if roles is None:
        print(f"Error: Unknown bundled role name: '{bundle_name}'. Please check BUNDLED_ROLES_CONFIG.", file=sys.stderr)
        sys.exit(1)
    return roles


def add_or_update_member_in_policy(policy_dict, role, member_type, member_email):
    """
    Adds or updates a member in a role binding within the policy dictionary.
    Returns the modified policy dictionary.
    """
    member_string = f"{member_type}:{member_email}"
    binding_found = False
    member_added_or_exists = False

    for binding in policy_dict.get("bindings", []):
        if binding.get("role") == role:
            binding_found = True
            if "members" not in binding: # Should not happen with valid policies
                binding["members"] = []
            
            # Ensure all existing members are valid strings
            binding["members"] = [str(m) for m in binding["members"] if isinstance(m, str)]

            if member_string not in binding["members"]:
                print(f"  Adding member '{member_string}' to existing role '{role}'.")
                binding["members"].append(member_string)
                member_added_or_exists = True
            else:
                print(f"  Member '{member_string}' already exists in role '{role}'. Skipping.")
                member_added_or_exists = True
            break # Role found and processed

    if not binding_found:
        print(f"  Adding new binding for role '{role}' with member '{member_string}'.")
        if "bindings" not in policy_dict:
            policy_dict["bindings"] = []
        policy_dict["bindings"].append({"role": role, "members": [member_string]})
        member_added_or_exists = True
    
    return policy_dict, member_added_or_exists


def main():
    parser = argparse.ArgumentParser(description="Assign IAM roles to a GCP Service Account and/or AD Group.")
    parser.add_argument("--mode", choices=["dry-run", "apply"], required=True, help="Operation mode.")
    parser.add_argument("--project-id", required=True, help="The GCP Project ID.")
    parser.add_argument("--service-account-email", help="Email of the GCP Service Account.")
    parser.add_argument("--ad-group-email", help="Email of the Active Directory Group.")
    parser.add_argument("--roles-sa", help="Comma-separated individual IAM roles for the Service Account.")
    parser.add_argument("--roles-ad", help="Comma-separated individual IAM roles for the AD Group.")
    parser.add_argument("--bundled-roles-sa", help="Name of a predefined bundled role for the Service Account.")
    parser.add_argument("--bundled-roles-ad", help="Name of a predefined bundled role for the AD Group.")

    args = parser.parse_args()

    if not args.service_account_email and not args.ad_group_email:
        parser.error("At least one of --service-account-email or --ad-group-email must be provided.")
    if args.service_account_email and not (args.roles_sa or args.bundled_roles_sa):
        parser.error("If --service-account-email is provided, at least one of --roles-sa or --bundled-roles-sa must also be provided.")
    if args.ad_group_email and not (args.roles_ad or args.bundled_roles_ad):
        parser.error("If --ad-group-email is provided, at least one of --roles-ad or --bundled-roles-ad must also be provided.")

    print(f"--- Starting IAM Role Assignment Script ({args.mode} mode) ---")
    print(f"Project ID: {args.project_id}")
    print(f"Service Account: {args.service_account_email or 'None'}")
    print(f"AD Group: {args.ad_group_email or 'None'}")
    print(f"Individual Roles for SA: {args.roles_sa or 'None'}")
    print(f"Bundled Role for SA: {args.bundled_roles_sa or 'None'}")
    print(f"Individual Roles for AD Group: {args.roles_ad or 'None'}")
    print(f"Bundled Role for AD Group: {args.bundled_roles_ad or 'None'}")
    print("------------------------------------------------------")

    print(f"Setting gcloud project to {args.project_id}...")
    # run_gcloud_command(["config", "set", "project", args.project_id], expect_json=False)
    # Setting CLOUDSDK_CORE_PROJECT in run_gcloud_command is preferred for atomicity

    print(f"Fetching current IAM policy for project {args.project_id}...")
    current_policy = run_gcloud_command(["projects", "get-iam-policy", args.project_id, "--format=json"], attempt_login=True)
    modified_policy = json.loads(json.dumps(current_policy)) # Deep copy

    any_changes_made = False

    # Process Service Account roles
    if args.service_account_email:
        sa_roles_to_assign = set()
        if args.roles_sa:
            sa_roles_to_assign.update(role.strip() for role in args.roles_sa.split(',') if role.strip())
        if args.bundled_roles_sa:
            sa_roles_to_assign.update(get_bundled_roles(args.bundled_roles_sa))
        
        if sa_roles_to_assign:
            print(f"Processing roles for Service Account: {args.service_account_email}")
            for role in sorted(list(sa_roles_to_assign)):
                modified_policy, changed = add_or_update_member_in_policy(modified_policy, role, "serviceAccount", args.service_account_email)
                if changed: any_changes_made = True
        else:
            print("No roles specified or resolved for Service Account. Skipping.")


    # Process AD Group roles
    if args.ad_group_email:
        ad_roles_to_assign = set()
        if args.roles_ad:
            ad_roles_to_assign.update(role.strip() for role in args.roles_ad.split(',') if role.strip())
        if args.bundled_roles_ad:
            ad_roles_to_assign.update(get_bundled_roles(args.bundled_roles_ad))

        if ad_roles_to_assign:
            print(f"Processing roles for AD Group: {args.ad_group_email}")
            for role in sorted(list(ad_roles_to_assign)):
                modified_policy, changed = add_or_update_member_in_policy(modified_policy, role, "group", args.ad_group_email)
                if changed: any_changes_made = True
        else:
            print("No roles specified or resolved for AD Group. Skipping.")
    
    # Ensure 'bindings' key exists if it was never created (e.g. empty policy and no roles added)
    if "bindings" not in modified_policy:
        modified_policy["bindings"] = []


    if args.mode == "dry-run":
        print("--- Dry Run Output (Proposed IAM Policy) ---")
        print(json.dumps(modified_policy, indent=2))
        print("--- End Dry Run Output ---")
        if not any_changes_made and json.dumps(current_policy.get("bindings",[]), sort_keys=True) == json.dumps(modified_policy.get("bindings",[]), sort_keys=True) :
            print("No changes proposed to the IAM policy.")
        else:
            print("Proposed changes are displayed above. No changes have been applied to GCP.")
    
    elif args.mode == "apply":
        # Only apply if there are actual changes to bindings or etag is different
        # A simple sort_keys comparison for bindings should be okay for detecting meaningful changes.
        # More robust would be a deep comparison ignoring member order within a binding, but this is often sufficient.
        current_bindings_str = json.dumps(current_policy.get("bindings", []), sort_keys=True)
        modified_bindings_str = json.dumps(modified_policy.get("bindings", []), sort_keys=True)

        if current_bindings_str == modified_bindings_str and current_policy.get("etag") == modified_policy.get("etag"):
            print("No effective changes to IAM policy bindings. Nothing to apply.")
        else:
            print(f"Applying modified IAM policy to project {args.project_id}...")
            
            # Ensure etag from the original policy is used for the update
            if "etag" not in current_policy:
                print("Error: ETag not found in current policy. Cannot apply changes safely.", file=sys.stderr)
                sys.exit(1)
            modified_policy["etag"] = current_policy["etag"] # Use original etag

            # Write the policy to a temporary file
            temp_policy_file_path = "temp_policy.json"
            with open(temp_policy_file_path, "w") as f:
                json.dump(modified_policy, f)
            
            print("Attempting to apply the following policy:")
            print(json.dumps(modified_policy, indent=2))

            try:
                applied_policy_result = run_gcloud_command(
                    ["projects", "set-iam-policy", args.project_id, temp_policy_file_path, "--format=json"]
                )
                print("IAM policy applied successfully.")
                print("New policy after application:")
                print(json.dumps(applied_policy_result, indent=2))
            finally:
                os.remove(temp_policy_file_path) # Clean up temp file
    
    print("--- Script Finished ---")

if __name__ == "__main__":
    main()
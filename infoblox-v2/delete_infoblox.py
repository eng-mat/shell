# This is your deletion script, now with debugging logs removed for security.
import argparse
import requests
import json
import os
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s]: %(message)s')
logger = logging.getLogger(__name__)

def get_infoblox_session(infoblox_url, username, password):
    """Establishes a session with Infoblox and handles authentication."""
    session = requests.Session()
    session.auth = (username, password)
    session.verify = False 
    return session

def find_network(session, infoblox_url, network_view, subnet_cidr):
    """Finds a specific network CIDR and logs its details for dry-run."""
    base_wapi_url = infoblox_url.rstrip('/')
    get_ref_url = f"{base_wapi_url}/network"
    logger.info(f"DRY-RUN: Searching for subnet '{subnet_cidr}' in view '{network_view}'...")
    
    get_ref_params = {
        "network_view": network_view,
        "network": subnet_cidr,
        "_return_fields+": "comment,extattrs"
    }
    
    try:
        response = session.get(get_ref_url, params=get_ref_params, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data and isinstance(data, list) and len(data) > 0 and '_ref' in data[0]:
            subnet_details = data[0]
            subnet_ref = subnet_details['_ref']
            logger.info("--- DRY-RUN: SUBNET FOUND ---")
            logger.info(f"  CIDR:           {subnet_details.get('network')}")
            logger.info(f"  Network View:   {subnet_details.get('network_view')}")
            logger.info(f"  Comment:        {subnet_details.get('comment', 'N/A')}")
            logger.info(f"  Ext. Attrs:     {subnet_details.get('extattrs', 'N/A')}")
            logger.info(f"  Internal Ref:   {subnet_ref}")
            logger.info("--- Review the details above before approving the 'apply' job. ---")
            return subnet_ref
        else:
            logger.error(f"ERROR: Could not find subnet '{subnet_cidr}' in view '{network_view}'.")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"ERROR: Infoblox API request failed while finding subnet: {e}")
        if response is not None:
             logger.error(f"Infoblox Response Content: {response.text}")
        return None

def delete_network(session, infoblox_url, subnet_ref):
    """Deletes a network using its specific _ref."""
    base_wapi_url = infoblox_url.rstrip('/')
    delete_url = f"{base_wapi_url}/{subnet_ref}"
    
    logger.info(f"APPLY: Sending DELETE request for object with reference: {subnet_ref}")

    try:
        response = session.delete(delete_url, timeout=30)
        response.raise_for_status()
        deleted_ref = response.json()
        logger.info(f"SUCCESS: Successfully deleted subnet. Infoblox returned ref: {deleted_ref}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"ERROR: Infoblox API request failed during deletion: {e}")
        if response is not None:
             logger.error(f"Infoblox Response Content: {response.text}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Infoblox CIDR Deletion Script with Dry-Run/Apply")
    subparsers = parser.add_subparsers(dest='action', required=True, help='Action to perform')
    
    parser_dryrun = subparsers.add_parser('dry-run', help='Find a subnet to be deleted and show its details.')
    parser_dryrun.add_argument("--infoblox-url", required=True)
    parser_dryrun.add_argument("--network-view", required=True)
    parser_dryrun.add_argument("--subnet-cidr", required=True)

    parser_apply = subparsers.add_parser('apply', help='Apply the deletion of a subnet using its reference.')
    parser_apply.add_argument("--infoblox-url", required=True)
    parser_apply.add_argument("--subnet-ref", required=True)

    args = parser.parse_args()

    infoblox_username = os.environ.get("INFOBLOX_USERNAME")
    infoblox_password = os.environ.get("INFOBLOX_PASSWORD")

    if not infoblox_username or not infoblox_password:
        logger.error("Infoblox username or password not found in environment variables.")
        exit(1)
        
    session = get_infoblox_session(args.infoblox_url, infoblox_username, infoblox_password)
    if not session:
        logger.error("Failed to establish Infoblox session.")
        exit(1)

    if args.action == 'dry-run':
        subnet_ref = find_network(session, args.infoblox_url, args.network_view, args.subnet_cidr)
        if subnet_ref:
            if 'GITHUB_OUTPUT' in os.environ:
                with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
                    print(f"subnet_ref={subnet_ref}", file=f)
            logger.info("\nDry-run successful. Output 'subnet_ref' has been set.")
        else:
            logger.error("\nDry-run failed. Subnet not found.")
            exit(1)
            
    elif args.action == 'apply':
        success = delete_network(session, args.infoblox_url, args.subnet_ref)
        if success:
            logger.info("\nApply successful. Subnet has been deleted.")
        else:
            logger.error("\nApply failed.")
            exit(1)

if __name__ == "__main__":
    main()

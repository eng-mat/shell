# This script creates a new CIDR reservation and correctly parses all arguments.
import argparse
import requests
import json
import ipaddress
import os
import logging

# Configure logging to be more verbose
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s]: %(message)s', force=True)
logger = logging.getLogger(__name__)

def get_infoblox_session(infoblox_url, username, password):
    """Establishes a session with Infoblox and handles authentication."""
    session = requests.Session()
    session.auth = (username, password)
    session.verify = False # For production, manage certificates properly.
    return session

def find_next_available_cidr(session, infoblox_url, network_view, supernet_ip, cidr_block_size):
    """
    Finds the next available CIDR block from a supernet container.
    """
    base_wapi_url = infoblox_url.rstrip('/')
    
    # Step 1: Get the _ref for the supernet (as a 'networkcontainer')
    get_ref_url = f"{base_wapi_url}/networkcontainer"
    logger.info(f"Searching for supernet container '{supernet_ip}' in view '{network_view}'...")
    
    get_ref_params = {
        "network_view": network_view,
        "network": supernet_ip
    }
    
    response = None
    supernet_ref = None
    try:
        response = session.get(get_ref_url, params=get_ref_params, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data and isinstance(data, list) and len(data) > 0 and '_ref' in data[0]:
            supernet_ref = data[0]['_ref']
            logger.info("Found supernet container reference.")
        else:
            logger.error(f"ERROR: Could not find a 'networkcontainer' object for '{supernet_ip}' in view '{network_view}'.")
            logger.error(f"Infoblox Response: {json.dumps(data)}")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"ERROR: API request failed while getting supernet _ref: {e}")
        if response is not None:
             logger.error(f"Infoblox Response Content: {response.text}")
        return None

    if not supernet_ref:
        return None

    # Step 2: POST to the _ref to call the next_available_network function
    post_func_url = f"{base_wapi_url}/{supernet_ref}"
    post_func_params = {"_function": "next_available_network"}
    post_func_payload = {"num": 1, "cidr": cidr_block_size}
    
    logger.info(f"Requesting next available /{cidr_block_size} subnet from the container...")
    response = None
    try:
        response = session.post(post_func_url, params=post_func_params, json=post_func_payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if data and isinstance(data, dict) and 'networks' in data and len(data['networks']) > 0:
            proposed_network = data['networks'][0]
            logger.info(f"Proposed network found: {proposed_network}")
            return proposed_network
        else:
            logger.error(f"ERROR: Could not find 'networks' in response from next_available_network call.")
            logger.error(f"Raw Infoblox Response: {json.dumps(data, indent=2)}")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"ERROR: API request failed while calling next_available_network: {e}")
        if response is not None:
            logger.error(f"Infoblox Response Content: {response.text}")
        return None

def reserve_cidr(session, infoblox_url, proposed_subnet, network_view, subnet_name, site_code):
    """Performs the actual CIDR reservation (network creation) in Infoblox."""
    wapi_url = f"{infoblox_url.rstrip('/')}/network"
    payload = {
        "network": proposed_subnet,
        "network_view": network_view,
        "comment": subnet_name,
        "extattrs": {
            "SiteCode": {"value": site_code}
        }
    }
    logger.info(f"Attempting to reserve CIDR: {proposed_subnet} in network view: {network_view}...")
    try:
        response = session.post(wapi_url, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        logger.info(f"SUCCESS: Successfully reserved CIDR: {proposed_subnet}. Infoblox Ref: {data}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"ERROR: API request failed during CIDR reservation: {e}")
        if response is not None:
             logger.error(f"Infoblox Response Content: {response.text}")
        return False

def get_supernet_info(session, infoblox_url, supernet_ip, network_view):
    """Simulated function to return supernet info."""
    return f"Information for supernet {supernet_ip} in network view {network_view} (simulation)"

def write_summary(args):
    """Writes a summary of the created reservation to the GitHub Job Summary."""
    if 'GITHUB_STEP_SUMMARY' in os.environ:
        logger.info("Writing reservation details to GitHub Job Summary.")
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            print("## ✅ Infoblox Reservation Successful", file=f)
            print("---", file=f)
            print("A new CIDR block has been successfully reserved with the following details:", file=f)
            print("", file=f)
            print(f"- **Subnet Name (Comment):** `{args.subnet_name}`", file=f)
            print(f"- **CIDR Range Reserved:** `{args.proposed_subnet}`", file=f)
            print(f"- **Network View:** `{args.network_view}`", file=f)
            print(f"- **Reserved From (Supernet):** `{args.supernet_ip}`", file=f)
            print(f"- **Site Code:** `{args.site_code}`", file=f)
            print("", file=f)
            print("You can view the new reservation in the Infoblox UI.", file=f)

def validate_inputs(network_view, supernet_ip, subnet_name, cidr_block_size_str):
    """Performs basic input validations and logs failures."""
    if not all([network_view, supernet_ip, subnet_name, str(cidr_block_size_str)]):
        logger.error("Validation Error: One or more required inputs are missing or empty.")
        return False
    try:
        cidr_block_size = int(cidr_block_size_str)
        if not (1 <= cidr_block_size <= 32):
            logger.error(f"Validation Error: CIDR block size must be an integer between 1 and 32. Received: {cidr_block_size}")
            return False
    except (ValueError, TypeError):
        logger.error(f"Validation Error: CIDR block size must be a valid integer. Received: '{cidr_block_size_str}'")
        return False
    try:
        ipaddress.ip_network(supernet_ip, strict=False)
    except ValueError:
        logger.error(f"Validation Error: Invalid Supernet IP format: '{supernet_ip}'")
        return False
    if not subnet_name.strip():
        logger.error("Validation Error: Subnet name cannot be empty or just whitespace.")
        return False
    
    logger.info("Input validation successful.")
    return True

def main():
    logger.info("--- Infoblox Reservation Script Starting ---")
    
    # This parser is designed for the CREATION workflow. It does NOT use subparsers.
    parser = argparse.ArgumentParser(description="Infoblox CIDR Reservation Workflow Script")
    parser.add_argument("action", choices=["dry-run", "apply"], help="Action to perform")
    parser.add_argument("--infoblox-url", required=True)
    parser.add_argument("--network-view", required=True)
    parser.add_argument("--supernet-ip", required=True)
    parser.add_argument("--subnet-name", required=True)
    parser.add_argument("--cidr-block-size", type=int, required=True)
    parser.add_argument("--site-code", required=False, default="GCP")
    parser.add_argument("--proposed-subnet", help="For apply action")
    parser.add_argument("--supernet-after-reservation", help="For apply action")
    
    try:
        args = parser.parse_args()
        logger.info(f"Action '{args.action}' selected.")
    except SystemExit as e:
        logger.error("Argument parsing failed. Please check the workflow inputs and script arguments.")
        # The argparse module will print the error message, so we just exit.
        raise e

    infoblox_username = os.environ.get("INFOBLOX_USERNAME")
    infoblox_password = os.environ.get("INFOBLOX_PASSWORD")
    if not infoblox_username or not infoblox_password:
        logger.error("Infoblox username or password not found in environment variables.")
        exit(1)

    if not validate_inputs(args.network_view, args.supernet_ip, args.subnet_name, args.cidr_block_size):
        logger.error("Input validation failed. Exiting.")
        exit(1)
        
    session = get_infoblox_session(args.infoblox_url, infoblox_username, infoblox_password)
    if not session:
        logger.error("Failed to establish Infoblox session.")
        exit(1)

    if args.action == "dry-run":
        logger.info("\n--- Performing Dry Run ---")
        proposed_subnet = find_next_available_cidr(session, args.infoblox_url, args.network_view, args.supernet_ip, args.cidr_block_size)
        if proposed_subnet:
            supernet_after_reservation = get_supernet_info(session, args.infoblox_url, args.supernet_ip, args.network_view)
            
            # Use the modern GITHUB_OUTPUT method to set job outputs
            if 'GITHUB_OUTPUT' in os.environ:
                with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
                    print(f"proposed_subnet={proposed_subnet}", file=f)
                    print(f"supernet_after_reservation={supernet_after_reservation}", file=f)
            logger.info("\nDry run completed successfully.")
        else:
            logger.error("DRY RUN FAILED: Could not determine a proposed subnet.")
            exit(1)

    elif args.action == "apply":
        logger.info("\n--- Performing Apply ---")
        if not args.proposed_subnet:
            logger.error("Apply FAILED: Missing --proposed-subnet from dry run.")
            exit(1)
        success = reserve_cidr(session, args.infoblox_url, args.proposed_subnet, args.network_view, args.subnet_name, args.site_code)
        if not success:
            logger.error("APPLY FAILED: Could not reserve CIDR.")
            exit(1)
        
        # Write the summary to the job summary page
        write_summary(args)
        logger.info("\nApply completed successfully.")

if __name__ == "__main__":
    main()

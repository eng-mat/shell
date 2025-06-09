import argparse
import requests
import json
import ipaddress
import os
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s]: %(message)s')
logger = logging.getLogger(__name__)

def get_infoblox_session(infoblox_url, username, password):
    """Establishes a session with Infoblox and handles authentication."""
    session = requests.Session()
    session.auth = (username, password)
    session.verify = False # For production, set to True and manage certs
    return session

def find_next_available_cidr(session, infoblox_url, network_view, supernet_ip, cidr_block_size):
    """
    Finds the next available CIDR block using a two-step process:
    1. GET the _ref for the supernet (as a networkcontainer).
    2. POST to the supernet's _ref to call _function=next_available_network.
    """
    base_wapi_url = infoblox_url.rstrip('/')
    
    get_ref_url = f"{base_wapi_url}/networkcontainer"
    logger.info(f"DEBUG SCRIPT: Step 1 - Getting _ref for supernet '{supernet_ip}' (as a networkcontainer) in view '{network_view}'")
    
    get_ref_params = {
        "network_view": network_view,
        "network": supernet_ip
    }
    logger.info(f"DEBUG SCRIPT: Params for getting ref: {json.dumps(get_ref_params)}")
    
    response = None
    supernet_ref = None
    try:
        response = session.get(get_ref_url, params=get_ref_params, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data and isinstance(data, list) and len(data) > 0 and '_ref' in data[0]:
            supernet_ref = data[0]['_ref']
            logger.info(f"DEBUG SCRIPT: Found supernet _ref: {supernet_ref}")
        else:
            logger.error(f"ERROR: Could not find _ref for supernet '{supernet_ip}' when searching for a 'networkcontainer'.")
            logger.error(f"Infoblox Response: {json.dumps(data)}")
            logger.error("VERIFICATION: Please ensure the supernet exists and is configured as a 'Network Container' in Infoblox.")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"ERROR: Infoblox API request failed during Step 1 (getting _ref): {e}")
        if response is not None:
             logger.error(f"Infoblox Response Content: {response.text}")
        return None

    if not supernet_ref:
        return None

    post_func_url = f"{base_wapi_url}/{supernet_ref}"
    
    post_func_params = {
        "_function": "next_available_network"
    }
    
    post_func_payload = {
        "num": 1,
        "cidr": cidr_block_size
    }
    
    logger.info(f"DEBUG SCRIPT: Step 2 - Calling 'next_available_network' function on _ref '{supernet_ref}'")
    logger.info(f"DEBUG SCRIPT:   Payload for POST: {json.dumps(post_func_payload)}")

    response = None
    try:
        response = session.post(post_func_url, params=post_func_params, json=post_func_payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if data and isinstance(data, dict) and 'networks' in data and len(data['networks']) > 0:
            proposed_network = data['networks'][0]
            logger.info(f"SUCCESS: Proposed network CIDR string found: {proposed_network}")
            return proposed_network
        else:
            logger.error(f"ERROR: Could not find 'networks' in response from next_available_network function call.")
            logger.error(f"Raw Infoblox Response: {json.dumps(data, indent=2)}")
            return None

    except requests.exceptions.HTTPError as http_err:
        logger.error(f"ERROR: Infoblox API request failed during Step 2 (calling function): {http_err}")
        if http_err.response is not None:
            logger.error(f"Infoblox Response Content: {http_err.response.text}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"ERROR: Infoblox API request failed (RequestException) during Step 2: {e}")
        if response is not None:
            logger.error(f"Infoblox Response Text (if available): {response.text}")
        return None
    except json.JSONDecodeError:
        logger.error(f"ERROR: Failed to parse JSON response from Infoblox during Step 2.")
        if response is not None:
             logger.error(f"Infoblox Raw Response: {response.text}")
        return None


def reserve_cidr(session, infoblox_url, proposed_subnet, network_view, subnet_name, site_code):
    """
    Performs the actual CIDR reservation (network creation) in Infoblox.
    """
    wapi_url = f"{infoblox_url.rstrip('/')}/network"

    payload = {
        "network": proposed_subnet,
        "network_view": network_view,
        "comment": subnet_name,
        "extattrs": {
            "Site Code": {"value": site_code}
        }
    }
    logger.info(f"Attempting to reserve CIDR: {proposed_subnet} in network view: {network_view}...")
    response = None
    try:
        response = session.post(wapi_url, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        logger.info(f"SUCCESS: Successfully reserved CIDR: {proposed_subnet}. Infoblox Ref: {data}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"ERROR: Infoblox API request failed during CIDR reservation: {e}")
        if response is not None:
             logger.error(f"Infoblox Response Content: {response.text}")
        return False

def get_supernet_info(session, infoblox_url, supernet_ip, network_view):
    logger.info(f"DEBUG SCRIPT: Simulating supernet info for {supernet_ip} in view {network_view}.")
    return f"Information for supernet {supernet_ip} in network view {network_view} (simulation)"

def validate_inputs(network_view, supernet_ip, subnet_name, cidr_block_size_str):
    if not all([network_view, supernet_ip, subnet_name, str(cidr_block_size_str)]):
        logger.error("Validation Error: All inputs (network_view, supernet_ip, subnet_name, cidr_block_size) are required.")
        return False
    try:
        cidr_block_size = int(cidr_block_size_str)
        if not (1 <= cidr_block_size <= 32):
            logger.error(f"Validation Error: CIDR block size must be an integer between 1 and 32. Received: {cidr_block_size}")
            return False
    except ValueError:
        logger.error(f"Validation Error: CIDR block size must be a valid integer. Received: {cidr_block_size_str}")
        return False
    try:
        ipaddress.ip_network(supernet_ip, strict=False)
    except ValueError:
        logger.error(f"Validation Error: Invalid Supernet IP format: {supernet_ip}")
        return False
    if not subnet_name.strip():
        logger.error("Validation Error: Subnet name cannot be empty or just whitespace.")
        return False
    return True

def main():
    parser = argparse.ArgumentParser(description="Infoblox CIDR Reservation Workflow Script")
    parser.add_argument("action", choices=["dry-run", "apply"], help="Action to perform: dry-run or apply")
    parser.add_argument("--infoblox-url", required=True, help="Infoblox WAPI URL (e.g., https://infoblox.example.com/wapi/vX.X)")
    parser.add_argument("--network-view", required=True, help="Infoblox Network View/Container")
    parser.add_argument("--supernet-ip", required=True, help="Supernet IP from which to reserve")
    parser.add_argument("--subnet-name", required=True, help="Name for the new subnet (used as comment)")
    parser.add_argument("--cidr-block-size", type=int, required=True, help="CIDR block size (e.g., 26 for /26)")
    parser.add_argument("--site-code", required=False, default="GCP", help="Site Code (default: GCP)")
    parser.add_argument("--proposed-subnet", help="Proposed subnet from dry-run (for apply action)")
    parser.add_argument("--supernet-after-reservation", help="Supernet status after reservation (from dry-run, simulated)")

    args = parser.parse_args()

    infoblox_username = os.environ.get("INFOBLOX_USERNAME")
    infoblox_password = os.environ.get("INFOBLOX_PASSWORD")

    if not infoblox_username or not infoblox_password:
        logger.error("Infoblox username or password not found in environment variables.")
        exit(1)

    if not validate_inputs(args.network_view, args.supernet_ip, args.subnet_name, args.cidr_block_size):
        exit(1)
        
    session = get_infoblox_session(args.infoblox_url, infoblox_username, infoblox_password)
    if not session:
        logger.error("Failed to establish Infoblox session.")
        exit(1)

    if args.action == "dry-run":
        logger.info("\n--- Performing Dry Run ---")
        proposed_subnet = find_next_available_cidr(
            session, args.infoblox_url, args.network_view, args.supernet_ip, args.cidr_block_size
        )

        if proposed_subnet:
            logger.info(f"DRY RUN: Proposed Subnet to Reserve: {proposed_subnet}")
            supernet_after_reservation = get_supernet_info(
                session, args.infoblox_url, args.supernet_ip, args.network_view
            )
            logger.info(f"DRY RUN: Supernet Status (simulated): {supernet_after_reservation}")

            # --- V V V THIS BLOCK IS THE ONLY CHANGE IN THIS SCRIPT V V V ---
            # Use the modern GITHUB_OUTPUT method to set job outputs
            if 'GITHUB_OUTPUT' in os.environ:
                logger.info(f"Setting outputs using GITHUB_OUTPUT.")
                with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
                    print(f"proposed_subnet={proposed_subnet}", file=f)
                    print(f"supernet_after_reservation={supernet_after_reservation}", file=f)
            else: # Fallback for older runners or local testing
                logger.warning("GITHUB_OUTPUT not found. Falling back to deprecated ::set-output.")
                print(f"::set-output name=proposed_subnet::{proposed_subnet}")
                print(f"::set-output name=supernet_after_reservation::{supernet_after_reservation}")
            # --- END OF CHANGE ---

            logger.info("\nDry run completed successfully.")
        else:
            logger.error("DRY RUN FAILED: Could not determine a proposed subnet.")
            exit(1)

    elif args.action == "apply":
        logger.info("\n--- Performing Apply ---")
        if not args.proposed_subnet:
            logger.error("Apply FAILED: Missing --proposed-subnet from dry run.")
            exit(1)
        
        success = reserve_cidr(
            session, args.infoblox_url, args.proposed_subnet, args.network_view, args.subnet_name, args.site_code
        )
        if success:
            logger.info(f"APPLY: Successfully reserved CIDR: {args.proposed_subnet}")
            logger.info("\nApply completed successfully.")
        else:
            logger.error(f"APPLY FAILED: Could not reserve CIDR: {args.proposed_subnet}.")
            exit(1)

if __name__ == "__main__":
    main()
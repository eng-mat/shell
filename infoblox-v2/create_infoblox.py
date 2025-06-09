# This is your creation script, now with debugging logs removed for security.
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
    session.verify = False 
    return session

def find_next_available_cidr(session, infoblox_url, network_view, supernet_ip, cidr_block_size):
    """Finds the next available CIDR block."""
    base_wapi_url = infoblox_url.rstrip('/')
    
    # Step 1: Get the _ref for the supernet (as a 'networkcontainer')
    get_ref_url = f"{base_wapi_url}/networkcontainer"
    logger.info(f"Searching for supernet container '{supernet_ip}' in view '{network_view}'...")
    
    get_ref_params = {"network_view": network_view, "network": supernet_ip}
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
            logger.error(f"ERROR: Could not find _ref for supernet container '{supernet_ip}'. Response: {json.dumps(data)}")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"ERROR: Infoblox API request failed while getting supernet _ref: {e}")
        if response is not None:
             logger.error(f"Infoblox Response Content: {response.text}")
        return None

    if not supernet_ref:
        return None

    # Step 2: POST to the _ref to call the next_available_network function
    post_func_url = f"{base_wapi_url}/{supernet_ref}"
    post_func_params = {"_function": "next_available_network"}
    post_func_payload = {"num": 1, "cidr": cidr_block_size}
    
    logger.info(f"Requesting next available /{cidr_block_size} from container...")
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
        logger.error(f"ERROR: Infoblox API request failed while calling next_available_network: {e}")
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
            "SiteCode": {"value": site_code} # As per your UI
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
        logger.error(f"ERROR: Infoblox API request failed during CIDR reservation: {e}")
        if response is not None:
             logger.error(f"Infoblox Response Content: {response.text}")
        return False

def get_supernet_info(session, infoblox_url, supernet_ip, network_view):
    return f"Information for supernet {supernet_ip} in network view {network_view} (simulation)"

def validate_inputs(network_view, supernet_ip, subnet_name, cidr_block_size_str):
    if not all([network_view, supernet_ip, subnet_name, str(cidr_block_size_str)]):
        return False
    try:
        if not (1 <= int(cidr_block_size_str) <= 32):
            return False
    except (ValueError, TypeError):
        return False
    try:
        ipaddress.ip_network(supernet_ip, strict=False)
    except ValueError:
        return False
    if not subnet_name.strip():
        return False
    return True

def main():
    # Argument parsing remains the same
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
    args = parser.parse_args()

    # Credentials fetching remains the same
    infoblox_username = os.environ.get("INFOBLOX_USERNAME")
    infoblox_password = os.environ.get("INFOBLOX_PASSWORD")
    if not infoblox_username or not infoblox_password:
        logger.error("Infoblox username or password not found in environment variables.")
        exit(1)

    if not validate_inputs(args.network_view, args.supernet_ip, args.subnet_name, args.cidr_block_size):
        logger.error("Input validation failed.")
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
            
            # Use the modern GITHUB_OUTPUT method
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
        logger.info("\nApply completed successfully.")

if __name__ == "__main__":
    main()

# This script intelligently reserves a CIDR block based on user-selected mode
# and loads its configuration from GitHub Variables.
import argparse
import requests
import json
import os
import logging

# The network view for all region-specific reservations remains configurable here.
REGION_SPECIFIC_NETWORK_VIEW = "network_view_spec"

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s]: %(message)s', force=True)
logger = logging.getLogger(__name__)

def load_mappings_from_env():
    """
    Loads the supernet mappings from a JSON string in an environment variable.
    """
    mappings_json = os.environ.get("SUPERNET_MAPPINGS_JSON")
    if not mappings_json:
        logger.error("FATAL: SUPERNET_MAPPINGS_JSON environment variable not found or is empty.")
        logger.error("Please ensure you have configured the 'SUPERNET_MAPPINGS_JSON' variable in your GitHub repository settings.")
        return None
    try:
        mappings = json.loads(mappings_json)
        logger.info("Successfully loaded supernet mappings from environment variable.")
        return mappings
    except json.JSONDecodeError as e:
        logger.error(f"FATAL: Failed to parse JSON from SUPERNET_MAPPINGS_JSON environment variable: {e}")
        logger.error("Please ensure the GitHub variable contains a valid JSON string.")
        return None

def get_infoblox_session(infoblox_url, username, password):
    session = requests.Session()
    session.auth = (username, password)
    session.verify = False
    return session

def find_next_available_cidr(session, infoblox_url, network_view, supernet_list, cidr_block_size):
    """
    Iterates through a list of supernets to find the first one with an available CIDR block.
    """
    for supernet_ip in supernet_list:
        logger.info(f"\nChecking supernet container '{supernet_ip}' in view '{network_view}'...")
        base_wapi_url = infoblox_url.rstrip('/')
        get_ref_url = f"{base_wapi_url}/networkcontainer"
        get_ref_params = {"network_view": network_view, "network": supernet_ip}
        
        supernet_ref = None
        try:
            response = session.get(get_ref_url, params=get_ref_params, timeout=30)
            response.raise_for_status()
            data = response.json()
            if data and isinstance(data, list) and len(data) > 0 and '_ref' in data[0]:
                supernet_ref = data[0]['_ref']
                logger.info(f"Found reference for '{supernet_ip}': {supernet_ref}")
            else:
                logger.warning(f"Could not find supernet container '{supernet_ip}' in view '{network_view}'. Trying next supernet.")
                continue
        except requests.exceptions.RequestException as e:
            logger.warning(f"API request failed while getting _ref for '{supernet_ip}': {e}. Trying next supernet.")
            continue
        
        # Step 2: Call the next_available_network function on the found _ref
        post_func_url = f"{base_wapi_url}/{supernet_ref}"
        post_func_params = {"_function": "next_available_network"}
        post_func_payload = {"num": 1, "cidr": cidr_block_size}
        
        logger.info(f"Requesting next available /{cidr_block_size} subnet from this container...")
        try:
            response = session.post(post_func_url, params=post_func_params, json=post_func_payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            if data and isinstance(data, dict) and 'networks' in data and len(data['networks']) > 0:
                proposed_network = data['networks'][0]
                logger.info(f"SUCCESS: Found available network '{proposed_network}' in supernet '{supernet_ip}'.")
                return proposed_network, supernet_ip # Return both the new CIDR and the supernet it came from
            else:
                logger.warning(f"No available /{cidr_block_size} networks in '{supernet_ip}'. Trying next supernet.")
                continue
        except requests.exceptions.RequestException as e:
            logger.warning(f"API request failed while calling next_available_network on '{supernet_ip}': {e}. Trying next supernet.")
            continue
            
    logger.error("Exhausted all supernets in the list. No available networks found.")
    return None, None


def reserve_cidr(session, infoblox_url, proposed_subnet, subnet_name, site_code):
    """Performs the actual CIDR reservation. The network view is derived from the subnet."""
    wapi_url = f"{infoblox_url.rstrip('/')}/network"
    payload = {
        "network": proposed_subnet,
        "comment": subnet_name,
        "extattrs": {
            "SiteCode": {"value": site_code}
        }
    }
    logger.info(f"Attempting to reserve CIDR: {proposed_subnet}...")
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

def main():
    # --- Load Mappings from Environment ---
    supernet_mappings = load_mappings_from_env()
    if supernet_mappings is None:
        exit(1) # Exit if mappings could not be loaded.

    parser = argparse.ArgumentParser(description="Infoblox Enhanced CIDR Reservation Script")
    subparsers = parser.add_subparsers(dest='action', required=True)

    # Parser for the 'dry-run' action
    parser_dryrun = subparsers.add_parser('dry-run', help='Find the next available CIDR based on reservation mode.')
    parser_dryrun.add_argument("--infoblox-url", required=True)
    parser_dryrun.add_argument("--reservation-mode", required=True, choices=['General', 'Region-Specific'])
    parser_dryrun.add_argument("--general-network-view", default="")
    parser_dryrun.add_argument("--region", default="")
    parser_dryrun.add_argument("--environment", default="")
    parser_dryrun.add_argument("--purpose", default="")
    parser_dryrun.add_argument("--subnet-name", required=True)
    parser_dryrun.add_argument("--cidr-block-size", type=int, required=True)
    parser_dryrun.add_argument("--site-code", required=True)

    # Parser for the 'apply' action
    parser_apply = subparsers.add_parser('apply', help='Reserve the CIDR found in the dry-run.')
    parser_apply.add_argument("--infoblox-url", required=True)
    parser_apply.add_argument("--proposed-subnet", required=True)
    parser_apply.add_argument("--subnet-name", required=True)
    parser_apply.add_argument("--site-code", required=True)
    
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

    if args.action == "dry-run":
        logger.info(f"\n--- Performing Dry Run for '{args.reservation_mode}' mode ---")
        
        network_view = None
        supernet_list = None

        if args.reservation_mode == 'General':
            if not args.general_network_view:
                logger.error("ERROR: For 'General' mode, a network view must be selected.")
                exit(1)
            network_view = args.general_network_view
            supernet_list = supernet_mappings.get(network_view)
            logger.info(f"Selected General Network View: '{network_view}'")
        
        elif args.reservation_mode == 'Region-Specific':
            if not all([args.region, args.environment, args.purpose]):
                logger.error("ERROR: For 'Region-Specific' mode, region, environment, and purpose must be selected.")
                exit(1)
            network_view = REGION_SPECIFIC_NETWORK_VIEW
            map_key = f"mg_{args.region}_{args.environment}_{args.purpose}"
            supernet_list = supernet_mappings.get(map_key)
            logger.info(f"Using Region-Specific Network View: '{network_view}'")
            logger.info(f"Constructed mapping key: '{map_key}'")

        if not supernet_list:
            logger.error(f"FATAL: No supernet mapping found for the selected criteria.")
            exit(1)

        logger.info(f"Searching for space in the following supernets: {supernet_list}")

        proposed_subnet, supernet_used = find_next_available_cidr(session, args.infoblox_url, network_view, supernet_list, args.cidr_block_size)
        
        if proposed_subnet:
            if 'GITHUB_OUTPUT' in os.environ:
                with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
                    print(f"proposed_subnet={proposed_subnet}", file=f)
                    print(f"supernet_used={supernet_used}", file=f)
            logger.info("\nDry run completed successfully.")
        else:
            logger.error("DRY RUN FAILED: Could not determine a proposed subnet from any of the mapped supernets.")
            exit(1)

    elif args.action == "apply":
        logger.info("\n--- Performing Apply ---")
        success = reserve_cidr(session, args.infoblox_url, args.proposed_subnet, args.subnet_name, args.site_code)
        if not success:
            logger.error("APPLY FAILED: Could not reserve CIDR.")
            exit(1)
        
        logger.info("\nApply completed successfully.")

if __name__ == "__main__":
    main()

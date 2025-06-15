# This script has been enhanced to handle multiple subnet purposes and sharing methods.
import argparse
import requests
import json
import os
import logging
import subprocess
import yaml

# --- Configuration ---
# Hardcoded mapping of user-friendly PSC project names to their actual GCP Project IDs.
# IMPORTANT: Update these placeholder IDs with your actual project IDs.
PSC_PROJECT_MAPPINGS = {
    "Non-Prod PSC Host 1": "your-psc-nonprod-project-1",
    "Non-Prod PSC Host 2": "your-psc-nonprod-project-2",
    "Non-Prod PSC Host 3": "your-psc-nonprod-project-3",
    "Non-Prod PSC Host 4": "your-psc-nonprod-project-4",
    "Prod PSC Host 1": "your-psc-prod-project-a",
    "Prod PSC Host 2": "your-psc-prod-project-b",
    "Prod PSC Host 3": "your-psc-prod-project-c",
    "Prod PSC Host 4": "your-psc-prod-project-d"
}

GKE_PODS_CIDR_SIZE = 24  # Default size for GKE Pods
GKE_SERVICES_CIDR_SIZE = 26 # Default size for GKE Services

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s]: %(message)s', force=True)
logger = logging.getLogger(__name__)

# --- Helper Functions ---
def load_infra_mappings():
    """Loads the infrastructure mappings from a JSON string in an environment variable."""
    mappings_json = os.environ.get("INFRA_MAPPINGS_JSON")
    if not mappings_json:
        logger.error("FATAL: INFRA_MAPPINGS_JSON environment variable not found or is empty.")
        return None
    try:
        return json.loads(mappings_json)
    except json.JSONDecodeError:
        logger.error("FATAL: Failed to parse JSON from INFRA_MAPPINGS_JSON environment variable.")
        return None

def run_command(command, check=True):
    """Runs a shell command and logs its output."""
    logger.info(f"Running command: {' '.join(command)}")
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=check)
        if result.stdout:
            logger.info(f"Command stdout:\n{result.stdout}")
        if result.stderr:
            logger.warning(f"Command stderr:\n{result.stderr}")
        return result
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed with exit code {e.returncode}")
        logger.error(f"stdout: {e.stdout}")
        logger.error(f"stderr: {e.stderr}")
        raise

# --- Infoblox Functions ---
def get_infoblox_session(infoblox_url, username, password):
    session = requests.Session()
    session.auth = (username, password)
    session.verify = False
    return session

def _find_one_available_cidr(session, infoblox_url, network_view, supernet_list, cidr_block_size):
    """Iterates through supernets to find one available CIDR block."""
    for supernet_ip in supernet_list:
        logger.info(f"Checking supernet container '{supernet_ip}' in view '{network_view}' for a /{cidr_block_size}...")
        base_wapi_url = infoblox_url.rstrip('/')
        get_ref_url = f"{base_wapi_url}/networkcontainer"
        get_ref_params = {"network_view": network_view, "network": supernet_ip}
        
        try:
            response = session.get(get_ref_url, params=get_ref_params, timeout=30)
            response.raise_for_status()
            data = response.json()
            if not (data and isinstance(data, list) and len(data) > 0 and '_ref' in data[0]):
                logger.warning(f"Could not find supernet container '{supernet_ip}'. Trying next.")
                continue
            
            supernet_ref = data[0]['_ref']
            
            post_func_url = f"{base_wapi_url}/{supernet_ref}"
            post_func_params = {"_function": "next_available_network"}
            post_func_payload = {"num": 1, "cidr": cidr_block_size}
            
            response = session.post(post_func_url, params=post_func_params, json=post_func_payload, timeout=30)
            response.raise_for_status()
            data = response.json()

            if data and isinstance(data, dict) and 'networks' in data and len(data['networks']) > 0:
                proposed_network = data['networks'][0]
                logger.info(f"SUCCESS: Found available network '{proposed_network}' in supernet '{supernet_ip}'.")
                return proposed_network, supernet_ip
        except requests.exceptions.RequestException as e:
            logger.warning(f"API request failed for supernet '{supernet_ip}': {e}. Trying next.")
            continue
            
    logger.error(f"Exhausted all supernets. No available /{cidr_block_size} networks found.")
    return None, None

def reserve_ips_in_infoblox(session, infoblox_url, infra_mappings, args):
    """High-level function to reserve one or more IPs based on subnet purpose."""
    logger.info(f"\n--- Starting Infoblox IP Reservation for purpose: '{args.subnet_purpose}' ---")
    
    network_config = infra_mappings.get(args.network_type)
    if not network_config:
        logger.error(f"FATAL: No configuration found for network_type '{args.network_type}' in mappings JSON.")
        return None, None, None, None, None, None

    host_project_id = network_config.get("host_project_id")
    vpc_name = network_config.get("vpc_name")
    primary_supernets = network_config.get("supernets")
    network_view = network_config.get("network_view")

    if not all([host_project_id, primary_supernets, network_view]):
        logger.error(f"FATAL: Incomplete configuration for '{args.network_type}'.")
        return None, None, None, None, None, None

    if args.subnet_purpose != 'PSC Endpoint' and not vpc_name:
        logger.error(f"FATAL: Configuration for '{args.network_type}' must include a 'vpc_name'.")
        return None, None, None, None, None, None

    # Reserve Primary CIDR
    logger.info(f"Reserving Primary CIDR (/{args.primary_cidr_size}) for '{args.subnet_name}'...")
    primary_cidr, supernet_used = _find_one_available_cidr(session, infoblox_url, network_view, primary_supernets, args.primary_cidr_size)
    if not primary_cidr:
        logger.error("Failed to reserve primary CIDR.")
        return None, None, None, None, None, None

    pods_cidr, services_cidr = None, None
    if args.subnet_purpose == 'GKE Cluster':
        non_routable_key = network_config.get("non_routable_key")
        if not non_routable_key:
            logger.error(f"FATAL: GKE request failed. The configuration for '{args.network_type}' is missing the 'non_routable_key' field in the mappings JSON.")
            return None, None, None, None, None, None
            
        non_routable_config = infra_mappings.get(non_routable_key)
        if not non_routable_config:
            logger.error(f"FATAL: GKE request failed. No configuration found for the non-routable key '{non_routable_key}'.")
            return None, None, None, None, None, None

        non_routable_supernets = non_routable_config.get("supernets")
        non_routable_view = non_routable_config.get("network_view")

        logger.info(f"Reserving Pods CIDR (/{args.gke_pods_cidr_size}) from '{non_routable_key}'...")
        pods_cidr, _ = _find_one_available_cidr(session, infoblox_url, non_routable_view, non_routable_supernets, args.gke_pods_cidr_size)
        
        logger.info(f"Reserving Services CIDR (/{args.gke_services_cidr_size}) from '{non_routable_key}'...")
        services_cidr, _ = _find_one_available_cidr(session, infoblox_url, non_routable_view, non_routable_supernets, args.gke_services_cidr_size)

        if not all([pods_cidr, services_cidr]):
            logger.error("Failed to reserve one or both secondary CIDRs for GKE.")
            # Note: A real implementation might need to "un-reserve" the primary CIDR here.
            return None, None, None, None, None, None

    return host_project_id, vpc_name, primary_cidr, pods_cidr, services_cidr, supernet_used

# --- GCP Functions ---
def create_subnet_in_gcp(args):
    """Creates a subnet in GCP using the gcloud CLI."""
    logger.info(f"\n--- Creating GCP Subnet '{args.subnet_name}' ---")
    
    command = [
        'gcloud', 'compute', 'networks', 'subnets', 'create', args.subnet_name,
        '--project', args.host_project_id,
        '--range', args.primary_cidr,
        '--region', args.region,
        '--enable-private-ip-google-access',
        '--enable-flow-logs',
        '--logging-aggregation-interval', 'interval-15-min',
        '--logging-flow-sampling', '0.5'
    ]
    
    if args.vpc_name:
        command.extend(['--network', args.vpc_name])

    if args.subnet_purpose == 'GKE Cluster':
        if not all([args.pods_cidr, args.services_cidr, args.gke_pods_range_name, args.gke_services_range_name]):
            logger.error("FATAL: For GKE cluster, all secondary range details (CIDRs and names) are required.")
            return False
        secondary_ranges = (
            f"{args.gke_pods_range_name}={args.pods_cidr},"
            f"{args.gke_services_range_name}={args.services_cidr}"
        )
        command.extend(['--secondary-range', secondary_ranges])
    
    elif args.subnet_purpose == 'PSC Endpoint':
        command.extend(['--purpose', 'PRIVATE_SERVICE_CONNECT'])
    
    try:
        run_command(command)
        logger.info("Successfully created GCP subnet.")
        return True
    except Exception as e:
        logger.error(f"Failed to create GCP subnet: {e}")
        return False

def share_subnet(args):
    """Shares the created subnet with either a Service Project or a PSC Host Project."""
    if args.subnet_purpose == 'PSC Endpoint':
        return share_subnet_with_psc_project(args)
    else:
        return share_subnet_with_service_project(args)

def share_subnet_with_psc_project(args):
    """Shares a PSC subnet by granting networkUser role to the PSC host project."""
    logger.info(f"\n--- Sharing PSC Subnet '{args.subnet_name}' ---")
    if not args.psc_host_project_name:
        logger.error("FATAL: For PSC purpose, a 'psc_host_project_name' must be selected.")
        return False

    psc_project_id = PSC_PROJECT_MAPPINGS.get(args.psc_host_project_name)
    if not psc_project_id:
        logger.error(f"FATAL: No PSC Project ID found in script mappings for '{args.psc_host_project_name}'.")
        return False

    # Get the project number for the PSC project to create the service account principal
    try:
        logger.info(f"Looking up project number for PSC project '{psc_project_id}'...")
        project_number = run_command(['gcloud', 'projects', 'describe', psc_project_id, '--format=value(projectNumber)']).stdout.strip()
        
        # This is a common service account format for the GKE Service Agent.
        # It's a robust choice for enabling services in another project to use this subnet.
        iam_member = f"serviceAccount:service-{project_number}@gcp-sa-gke.iam.gserviceaccount.com"
        logger.info(f"Sharing subnet with IAM member: {iam_member}")

        command = [
            'gcloud', 'compute', 'networks', 'subnets', 'add-iam-policy-binding', args.subnet_name,
            '--project', args.host_project_id,
            '--region', args.region,
            '--member', iam_member,
            '--role', 'roles/compute.networkUser'
        ]
        run_command(command)
        logger.info(f"Successfully shared PSC subnet with project '{psc_project_id}'.")
        return True
    except Exception as e:
        logger.error(f"Failed to share PSC subnet: {e}")
        return False

def share_subnet_with_service_project(args):
    """Shares the created subnet with a service project by updating its org policy."""
    logger.info(f"\n--- Sharing Subnet '{args.subnet_name}' with Service Project '{args.service_project_id}' ---")
    if not args.service_project_id:
        logger.error("FATAL: For this purpose, a 'service_project_id' must be provided.")
        return False

    subnet_path = f"projects/{args.host_project_id}/regions/{args.region}/subnetworks/{args.subnet_name}"
    
    try:
        logger.info(f"Fetching current 'restrictSharedVpcSubnetworks' policy for project '{args.service_project_id}'...")
        describe_command = ['gcloud', 'org-policies', 'describe', 'compute.restrictSharedVpcSubnetworks', '--project', args.service_project_id, '--format', 'yaml']
        result = run_command(describe_command, check=False)
        
        policy_data = yaml.safe_load(result.stdout) if result.returncode == 0 and result.stdout else \
            {'name': f"projects/{args.service_project_id}/policies/compute.restrictSharedVpcSubnetworks",
             'spec': {'rules': [{'values': {'allowedValues': []}}]}}
            
        rules = policy_data.setdefault('spec', {}).setdefault('rules', [{'values': {'allowedValues': []}}])
        allowed_values = rules[0].setdefault('values', {}).setdefault('allowedValues', [])
        
        if subnet_path not in allowed_values:
            logger.info(f"Adding '{subnet_path}' to the list of allowed subnets.")
            allowed_values.append(subnet_path)
        else:
            logger.info(f"Subnet '{subnet_path}' is already in the policy. No changes needed.")
            return True

        new_policy_file = 'new_policy.yaml'
        with open(new_policy_file, 'w') as f:
            yaml.dump(policy_data, f)
        
        set_policy_command = ['gcloud', 'org-policies', 'set-policy', new_policy_file, '--project', args.service_project_id]
        run_command(set_policy_command)
        
        logger.info("Successfully updated org policy to share subnet.")
        return True
    except Exception as e:
        logger.error(f"Failed to share subnet with service project: {e}")
        return False

# --- Main Execution ---
def main():
    parser = argparse.ArgumentParser(description="Infoblox & GCP End-to-End Subnet Automation")
    subparsers = parser.add_subparsers(dest='action', required=True)

    # --- Parser for 'reserve-ips' action ---
    p_reserve = subparsers.add_parser('reserve-ips', help='Reserve IP(s) in Infoblox.')
    p_reserve.add_argument("--infoblox-url", required=True)
    p_reserve.add_argument("--subnet-purpose", required=True)
    p_reserve.add_argument("--network-type", required=True)
    p_reserve.add_argument("--subnet-name", required=True)
    p_reserve.add_argument("--primary-cidr-size", type=int, required=True)
    p_reserve.add_argument("--gke-pods-cidr-size", type=int, default=GKE_PODS_CIDR_SIZE)
    p_reserve.add_argument("--gke-services-cidr-size", type=int, default=GKE_SERVICES_CIDR_SIZE)
    
    # --- Parser for 'create-and-share-subnet' action ---
    p_create = subparsers.add_parser('create-and-share-subnet', help='Create subnet in GCP and share it.')
    p_create.add_argument("--subnet-purpose", required=True)
    p_create.add_argument("--host-project-id", required=True)
    p_create.add_argument("--vpc-name")
    p_create.add_argument("--region", required=True)
    p_create.add_argument("--service-project-id")
    p_create.add_argument("--psc-host-project-name")
    p_create.add_argument("--subnet-name", required=True)
    p_create.add_argument("--primary-cidr", required=True)
    p_create.add_argument("--pods-cidr", default="")
    p_create.add_argument("--services-cidr", default="")
    p_create.add_argument("--gke-pods-range-name", default="")
    p_create.add_argument("--gke-services-range-name", default="")
    
    args = parser.parse_args()
    
    if args.action == 'reserve-ips':
        infra_mappings = load_infra_mappings()
        if not infra_mappings: exit(1)

        username = os.environ.get("INFOBLOX_USERNAME")
        password = os.environ.get("INFOBLOX_PASSWORD")
        if not username or not password: exit(1)

        session = get_infoblox_session(args.infoblox_url, username, password)
        host_project_id, vpc_name, primary_cidr, pods_cidr, services_cidr, supernet_used = reserve_ips_in_infoblox(session, args.infoblox_url, infra_mappings, args)

        if not primary_cidr:
            logger.error("Dry-run failed: Could not reserve primary CIDR from Infoblox.")
            exit(1)
            
        if 'GITHUB_OUTPUT' in os.environ:
            with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
                print(f"host_project_id={host_project_id}", file=f)
                print(f"vpc_name={vpc_name or ''}", file=f)
                print(f"primary_cidr={primary_cidr}", file=f)
                print(f"pods_cidr={pods_cidr or ''}", file=f)
                print(f"services_cidr={services_cidr or ''}", file=f)
                print(f"supernet_used={supernet_used or ''}", file=f)
        logger.info("\nDry-run (IP reservation) completed successfully.")

    elif args.action == 'create-and-share-subnet':
        if not create_subnet_in_gcp(args):
            logger.error("Apply failed: Could not create subnet in GCP.")
            exit(1)
        
        if not share_subnet(args):
            logger.error("Apply failed: Could not share subnet.")
            exit(1)

        logger.info("\nApply (GCP subnet creation and sharing) completed successfully.")

if __name__ == "__main__":
    main()

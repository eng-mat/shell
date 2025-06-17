#!/bin/bash

# This script calculates derived variables, sets required IAM policies, and deploys a GCS bucket and Vertex AI Notebook.
# It can operate in dry-run mode or apply mode based on the first argument.

# Exit immediately if a command exits with a non-zero status.
set -e

# --- Script Arguments ---
MODE="$1" # Expected: "--dry-run" or "--apply"

# Trim whitespace/hidden characters from MODE
MODE=$(echo "$MODE" | xargs)

# DEBUG: Print the argument received and the MODE variable
echo "DEBUG: Argument 1 received: '$1'"
echo "DEBUG: MODE variable set to: '$MODE'"

# --- Required Environment Variables (passed from GitHub Actions) ---
# SERVICE_PROJECT_ID, ENVIRONMENT_TYPE, REGION, VPC_NAME, SUBNET_NAME,
# INSTANCE_OWNER_EMAIL, USER_AD_GROUP_EMAIL, LAST_NAME_FIRST_INITIAL, MACHINE_TYPE

# Validate required environment variables
if [ -z "$SERVICE_PROJECT_ID" ] || \
   [ -z "$ENVIRONMENT_TYPE" ] || \
   [ -z "$REGION" ] || \
   [ -z "$VPC_NAME" ] || \
   [ -z "$SUBNET_NAME" ] || \
   [ -z "$INSTANCE_OWNER_EMAIL" ] || \
   [ -z "$USER_AD_GROUP_EMAIL" ] || \
   [ -z "$LAST_NAME_FIRST_INITIAL" ]; then
  echo "Error: One or more required environment variables are not set."
  exit 1
fi

# Set default for MACHINE_TYPE if not provided
if [ -z "$MACHINE_TYPE" ]; then
  MACHINE_TYPE="e2-standard-4"
fi

echo "--- Inputs Received ---"
echo "SERVICE_PROJECT_ID: $SERVICE_PROJECT_ID"
echo "ENVIRONMENT_TYPE: $ENVIRONMENT_TYPE"
echo "REGION: $REGION"
echo "VPC_NAME: $VPC_NAME"
echo "SUBNET_NAME: $SUBNET_NAME"
echo "INSTANCE_OWNER_EMAIL: $INSTANCE_OWNER_EMAIL"
echo "USER_AD_GROUP_EMAIL: $USER_AD_GROUP_EMAIL"
echo "LAST_NAME_FIRST_INITIAL: $LAST_NAME_FIRST_INITIAL"
echo "MACHINE_TYPE: $MACHINE_TYPE"
echo "MODE: $MODE"
echo "-----------------------"

# --- Input Format Validations ---
if ! [[ "$SERVICE_PROJECT_ID" =~ ^[a-z0-9-]+-(poc|ppoc)-[a-z0-9.-]+$ ]]; then
  echo "Error: SERVICE_PROJECT_ID '$SERVICE_PROJECT_ID' does not follow the expected format (e.g., test-poc-my-project)."
  exit 1
fi
if ! [[ "$INSTANCE_OWNER_EMAIL" =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]]; then
  echo "Error: INSTANCE_OWNER_EMAIL '$INSTANCE_OWNER_EMAIL' is not a valid email address format."
  exit 1
fi
if ! [[ "$USER_AD_GROUP_EMAIL" =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]]; then
  echo "Error: USER_AD_GROUP_EMAIL '$USER_AD_GROUP_EMAIL' is not a valid email address format."
  exit 1
fi
if ! [[ "$LAST_NAME_FIRST_INITIAL" =~ ^[a-z]+-[a-z]$ ]]; then
  echo "Error: LAST_NAME_FIRST_INITIAL '$LAST_NAME_FIRST_INITIAL' does not follow the expected format (e.g., smith-j)."
  exit 1
fi


# --- Determine Host Project ID and Network Tag based on VPC name mapping ---
HOST_PROJECT_ID=""
NETWORK_TAG=""

case "$VPC_NAME" in
  "vpc-ss-tru-nonprod-7")
    HOST_PROJECT_ID="akilapa-vpc"
    NETWORK_TAG="tag-ss-nonprod"
    ;;
  "vpc-ss-tru-prod-7")
    HOST_PROJECT_ID="akilapa-vpc"
    NETWORK_TAG="tag-ss-prod"
    ;;
  "vpc-jjk-tru-nonprod-7")
    HOST_PROJECT_ID="vpc-elese-aki"
    NETWORK_TAG="tag-jjk-nonprod"
    ;;
  "vpc-jjk-tru-prod-7")
    HOST_PROJECT_ID="vpc-elese-aki"
    NETWORK_TAG="tag-jjk-prod"
    ;;
  "vpc-jjkkula-tru-nonprod-7")
    HOST_PROJECT_ID="aya-kudelo-pak"
    NETWORK_TAG="tag-jjkkula-nonprod"
    ;;
  "vpc-jjkkula-tru-prod-7")
    HOST_PROJECT_ID="aya-kudelo-pak"
    NETWORK_TAG="tag-jjkkula-prod"
    ;;
  *)
    echo "Error: Could not determine host project and network tag for VPC name '$VPC_NAME'. Please check VPC name selection."
    exit 1
    ;;
esac
echo "Determined Host Project ID: $HOST_PROJECT_ID"
echo "Determined Network Tag: $NETWORK_TAG"

# --- Construct full network and subnet resource paths ---
FULL_NETWORK="projects/${HOST_PROJECT_ID}/global/networks/${VPC_NAME}"
SUBNET_RESOURCE="projects/${HOST_PROJECT_ID}/regions/${REGION}/subnetworks/${SUBNET_NAME}"
echo "Full Network Path: $FULL_NETWORK"
echo "Subnet Resource Path: $SUBNET_RESOURCE"

# --- Determine Vertex AI Notebook Zone ---
ZONE="${REGION}-a"
echo "Vertex AI Notebook Zone: $ZONE"

# --- Construct Vertex AI Notebook Name ---
NOTEBOOK_NAME="${LAST_NAME_FIRST_INITIAL}-notebook"
echo "Vertex AI Notebook Name: $NOTEBOOK_NAME"

# --- Construct Service Account and Service Agent Names ---
VERTEX_SA="vertexai-sa@${SERVICE_PROJECT_ID}.iam.gserviceaccount.com"
echo "Vertex AI Service Account: $VERTEX_SA"
echo "Determining Service Project Number..."
SERVICE_PROJECT_NUMBER=$(gcloud projects describe "$SERVICE_PROJECT_ID" --format="value(projectNumber)")
NOTEBOOKS_SERVICE_AGENT="service-${SERVICE_PROJECT_NUMBER}@gcp-sa-notebooks.iam.gserviceaccount.com"
echo "Notebooks API Service Agent: $NOTEBOOKS_SERVICE_AGENT"

# --- Construct CMEK Key for Notebook and GCS Bucket ---
CMEK_VAULT_PROJECT="my-key-${ENVIRONMENT_TYPE}"
CMEK_KEY_RING="key-${ENVIRONMENT_TYPE}-${REGION}-my-${SERVICE_PROJECT_ID}-${REGION}"
CMEK_KEY="projects/${CMEK_VAULT_PROJECT}/locations/${REGION}/keyRings/${CMEK_KEY_RING}/cryptoKeys/key-${ENVIRONMENT_TYPE}-${REGION}-my-${SERVICE_PROJECT_ID}-${REGION}"
echo "CMEK Key: $CMEK_KEY"

# --- Construct GCS Bucket Name and its CMEK Key ---
GCS_BUCKET_NAME="vertex-gcs-${SERVICE_PROJECT_ID}"
GCS_CMEK_KEY="${CMEK_KEY}"
echo "GCS Bucket Name: $GCS_BUCKET_NAME"
echo "GCS Bucket CMEK Key: $GCS_CMEK_KEY"

# --- Define Post-Startup Script for SSH Hardening ---
# Using command substitution with a heredoc for maximum portability.
# The 'EOF' is quoted to prevent any local shell variable expansion.
STARTUP_SCRIPT=$(cat <<'EOF'
#!/bin/bash
set -e
echo "--- Starting SSH hardening script ---"
# Define the SSH hardening configuration
SSH_CONFIG_BLOCK="
# --- Harden SSH Ciphers, KEX, and MACs ---
KexAlgorithms curve25519-sha256@libssh.org,ecdh-sha2-nistp521,ecdh-sha2-nistp384,ecdh-sha2-nistp256,diffie-hellman-group-exchange-sha256
Ciphers aes256-gcm@openssh.com,aes128-gcm@openssh.com,aes256-ctr,aes192-ctr,aes128-ctr
MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com,hmac-sha2-512,hmac-sha2-256
"
# Append the configuration to the main sshd_config file
echo "${SSH_CONFIG_BLOCK}" | sudo tee -a /etc/ssh/sshd_config > /dev/null
echo "Appended strong SSH configuration to /etc/ssh/sshd_config."
# Restart the SSH service to apply the new configuration
sudo systemctl restart sshd
echo "SSH service restarted. Hardening script complete. ---"
EOF
)

# --- Execute gcloud commands based on MODE ---
if [ "$MODE" == "--dry-run" ]; then
  echo "--- Performing Dry Run for All Actions ---"

  echo "Simulating: Ensure Vertex AI Service Account '${VERTEX_SA}' exists..."
  echo "--- Simulating IAM Policy Bindings ---"
  echo "  - Simulating: Grant 'roles/iam.serviceAccountUser' to group '${USER_AD_GROUP_EMAIL}' on SA '${VERTEX_SA}' in project '${SERVICE_PROJECT_ID}'."
  echo "  - Simulating: Grant 'roles/compute.networkUser' on subnet '${SUBNET_NAME}' in host project '${HOST_PROJECT_ID}' to:"
  echo "    - Vertex AI SA: '${VERTEX_SA}'"
  echo "    - User AD Group: '${USER_AD_GROUP_EMAIL}'"
  echo "    - Notebooks Service Agent: '${NOTEBOOKS_SERVICE_AGENT}'"
  echo "--- Simulating GCS Bucket Creation ---"
  if ! gcloud storage buckets describe "gs://${GCS_BUCKET_NAME}" --project="${SERVICE_PROJECT_ID}" &> /dev/null; then
    echo "Simulating: gcloud storage buckets create \"gs://${GCS_BUCKET_NAME}\" ..."
  else
    echo "GCS bucket 'gs://${GCS_BUCKET_NAME}' already exists. Skipping dry run for creation."
  fi

  # Simulate Vertex AI Notebook Creation
  echo "--- Simulating Vertex AI Notebook Creation ---"
  if ! gcloud workbench instances describe "${NOTEBOOK_NAME}" --project="${SERVICE_PROJECT_ID}" --location="${ZONE}" &> /dev/null; then
    echo "Simulating: gcloud workbench instances create \"${NOTEBOOK_NAME}\" with network tag '${NETWORK_TAG}' and applying post-startup script..."
  else
    echo "Vertex AI Notebook instance '${NOTEBOOK_NAME}' already exists. Skipping dry run for creation."
  fi

elif [ "$MODE" == "--apply" ]; then
  # Apply Service Account Creation
  echo "--- Applying: Ensuring Vertex AI Service Account exists ---"
  if ! gcloud iam service-accounts describe "${VERTEX_SA}" --project="${SERVICE_PROJECT_ID}" &> /dev/null; then
    echo "Service Account '${VERTEX_SA}' not found. Creating it..."
    gcloud iam service-accounts create "$(echo ${VERTEX_SA} | cut -d'@' -f1)" \
      --project="${SERVICE_PROJECT_ID}" \
      --display-name="Custom Vertex AI Notebook SA"
  else
    echo "Service Account '${VERTEX_SA}' already exists. Skipping creation."
  fi

  # Apply IAM policies
  echo "--- Applying: Required IAM policies ---"
  gcloud iam service-accounts add-iam-policy-binding "${VERTEX_SA}" \
    --project="${SERVICE_PROJECT_ID}" --member="group:${USER_AD_GROUP_EMAIL}" --role="roles/iam.serviceAccountUser"
  gcloud compute networks subnets add-iam-policy-binding "${SUBNET_NAME}" \
    --project="${HOST_PROJECT_ID}" --region="${REGION}" --member="serviceAccount:${VERTEX_SA}" --role="roles/compute.networkUser"
  gcloud compute networks subnets add-iam-policy-binding "${SUBNET_NAME}" \
    --project="${HOST_PROJECT_ID}" --region="${REGION}" --member="group:${USER_AD_GROUP_EMAIL}" --role="roles/compute.networkUser"
  gcloud compute networks subnets add-iam-policy-binding "${SUBNET_NAME}" \
    --project="${HOST_PROJECT_ID}" --region="${REGION}" --member="serviceAccount:${NOTEBOOKS_SERVICE_AGENT}" --role="roles/compute.networkUser"
  echo "IAM policies applied."

  # Apply GCS Bucket Creation
  echo "--- Applying: GCS Bucket Creation ---"
  if ! gcloud storage buckets describe "gs://${GCS_BUCKET_NAME}" --project="${SERVICE_PROJECT_ID}" &> /dev/null; then
    echo "GCS bucket 'gs://${GCS_BUCKET_NAME}' not found. Proceeding with creation."
    gcloud storage buckets create "gs://${GCS_BUCKET_NAME}" \
      --project="${SERVICE_PROJECT_ID}" --location="${REGION}" --default-kms-key="${GCS_CMEK_KEY}" --uniform-bucket-level-access
  else
    echo "GCS bucket 'gs://${GCS_BUCKET_NAME}' already exists. Skipping creation."
  fi

  # Apply Vertex AI Notebook Creation
  echo "--- Applying: Vertex AI Notebook Creation ---"
  if ! gcloud workbench instances describe "${NOTEBOOK_NAME}" --project="${SERVICE_PROJECT_ID}" --location="${ZONE}" &> /dev/null; then
    echo "Vertex AI Notebook instance '${NOTEBOOK_NAME}' not found. Proceeding with creation."
    gcloud workbench instances create "${NOTEBOOK_NAME}" \
      --project="${SERVICE_PROJECT_ID}" \
      --location="${ZONE}" \
      --machine-type="${MACHINE_TYPE}" \
      --boot-disk-size=150GB \
      --data-disk-size=100GB \
      --subnet="${SUBNET_RESOURCE}" \
      --network="${FULL_NETWORK}" \
      --service-account="${VERTEX_SA}" \
      --tags="${NETWORK_TAG}" \
      --no-enable-public-ip \
      --no-enable-realtime-in-terminal \
      --owner="${INSTANCE_OWNER_EMAIL}" \
      --enable-notebook-upgrade-scheduling \
      --notebook-upgrade-schedule="WEEKLY:SATURDAY:21:00" \
      --metadata="enable-root-access=true,startup-script=${STARTUP_SCRIPT}" \
      --kms-key="${CMEK_KEY}" \
      --no-shielded-secure-boot \
      --shielded-integrity-monitoring \
      --shielded-vtpm
  else
    echo "Vertex AI Notebook instance '${NOTEBOOK_NAME}' already exists. Skipping creation."
  fi

else
  echo "Error: Invalid mode. Use '--dry-run' or '--apply'."
  exit 1
fi

echo "Script execution complete."
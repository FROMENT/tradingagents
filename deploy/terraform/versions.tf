terraform {
  required_version = ">= 1.5.0"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 5.0.0"
    }
  }

  # Recommended: keep state off git in OCI Object Storage (S3-compatible), since
  # tfstate can contain sensitive values. Fill in and uncomment.
  # backend "s3" {
  #   bucket                      = "tf-state-tradingagents"
  #   key                         = "intraday/terraform.tfstate"
  #   region                      = "eu-frankfurt-1"
  #   endpoints                   = { s3 = "https://<namespace>.compat.objectstorage.eu-frankfurt-1.oraclecloud.com" }
  #   skip_region_validation      = true
  #   skip_credentials_validation = true
  #   skip_metadata_api_check     = true
  #   skip_requesting_account_id  = true
  #   use_path_style              = true
  # }
}

provider "oci" {
  tenancy_ocid     = var.tenancy_ocid
  user_ocid        = var.user_ocid
  fingerprint      = var.fingerprint
  private_key_path = var.private_key_path
  region           = var.region
}

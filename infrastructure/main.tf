terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }

  backend "s3" {
    bucket         = "alerce-terraform-state"
    key            = "status-website/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "alerce-terraform-state-lock"
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

locals {
  name_prefix = "alerce-status-${var.environment}"
}

# {{T:tf_header}}
# Maintainer: {{PERSON_1}} <{{EMAIL_1}}>

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "eu-west-3"
}

variable "victoria_cluster_name" {
  description = "{{T:tf_var_desc}}"
  type        = string
  default     = "victoria-metrics"
}

resource "aws_iam_role" "admin" {
  name = "jordan-ci-admin"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
  tags = {
    Owner      = "{{PERSON_2}}"
    Office     = "{{CITY_1}}"
    CostCenter = "platform"
  }
}

resource "aws_instance" "paris_gateway" {
  ami               = "ami-0c55b159cbfafe1f0"
  instance_type     = "t3.micro"
  availability_zone = "eu-west-3a"
  subnet_id         = null
  tags = {
    Name = "paris-gateway"
    # {{T:tf_contact}}
    Contact = "{{PERSON_3}}"
    Site    = "{{CITY_2}}"
  }
}

output "gateway_ip" {
  value = aws_instance.paris_gateway.public_ip
}

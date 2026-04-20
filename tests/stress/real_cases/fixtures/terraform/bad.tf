# Deliberately insecure Terraform — used as fixture for iac_guardrails_scan tests.
# Expected findings: TF-002 (S3 public ACL), TF-004 (SSH 0.0.0.0/0),
# TF-001 (SG open-all), TF-003 (RDS publicly accessible).

resource "aws_s3_bucket" "public_assets" {
  bucket = "my-public-assets"
  acl    = "public-read"
}

resource "aws_security_group" "open_ssh" {
  name = "ssh-everywhere"

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 0
    to_port     = 65535
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_instance" "exposed_rds" {
  identifier           = "exposed-db"
  engine               = "postgres"
  instance_class       = "db.t3.micro"
  publicly_accessible  = true
  skip_final_snapshot  = true
}

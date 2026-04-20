# Intentionally clean Terraform — used as fixture to verify zero critical findings.

resource "aws_s3_bucket" "private_assets" {
  bucket = "my-private-assets"
  acl    = "private"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "private_assets" {
  bucket = aws_s3_bucket.private_assets.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_security_group" "restricted" {
  name = "ssh-from-bastion"

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["10.0.1.0/24"]
  }
}

resource "aws_db_instance" "private_rds" {
  identifier          = "private-db"
  engine              = "postgres"
  instance_class      = "db.t3.micro"
  publicly_accessible = false
  storage_encrypted   = true
  skip_final_snapshot = true
}

"""
Terraform Scanner for IaC Guardrails.

Scans Terraform HCL files for security issues.
"""
import re
from typing import List, Dict, Any
from . import BaseScanner, IacFinding


class TerraformScanner(BaseScanner):
	"""Scanner for Terraform configuration."""

	def scan(self, content: str, filepath: str, profile: str) -> List[IacFinding]:
		"""Scan Terraform configuration for security issues."""
		findings = []
		lines = content.split('\n')

		findings.extend(self._scan_security_groups(content, filepath, lines))
		findings.extend(self._scan_s3_buckets(content, filepath, lines))
		findings.extend(self._scan_rds(content, filepath, lines))
		findings.extend(self._scan_iam(content, filepath, lines))
		findings.extend(self._scan_encryption(content, filepath, lines))

		return findings

	def _scan_security_groups(self, content: str, filepath: str, lines: List[str]) -> List[IacFinding]:
		"""Scan for overly permissive security groups."""
		findings = []

		# Check for 0.0.0.0/0 in ingress rules
		pattern = r'cidr_blocks\s*=\s*\[\s*"0\.0\.0\.0/0"\s*\]'
		for match in re.finditer(pattern, content, re.MULTILINE):
			line_num = content[:match.start()].count('\n') + 1
			findings.append(IacFinding(
				rule_id='TF-001',
				rule_title='Security group allows all inbound traffic',
				severity='critical',
				path=filepath,
				line=line_num,
				message="Security group allows traffic from 0.0.0.0/0 (any IP)",
				remediation="Restrict cidr_blocks to specific IPs or ranges",
				references=['https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/security_group'],
				engine="tf-scanner"
			))

		# Check for open SSH (port 22) from internet
		ssh_pattern = r'(?:from_port|to_port)\s*=\s*22[\s\S]*?cidr_blocks\s*=\s*\[\s*"0\.0\.0\.0/0"\s*\]'
		if re.search(ssh_pattern, content):
			findings.append(IacFinding(
				rule_id='TF-004',
				rule_title='SSH port open to world',
				severity='critical',
				path=filepath,
				line=0,
				message="SSH port (22) is open to the entire internet",
				remediation="Restrict SSH access to trusted IPs or use a bastion host",
				references=['https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/authorizing-access-to-an-instance.html'],
				engine="tf-scanner"
			))

		return findings

	def _scan_s3_buckets(self, content: str, filepath: str, lines: List[str]) -> List[IacFinding]:
		"""Scan for public S3 bucket configurations."""
		findings = []

		# Check for public-read ACL
		pattern = r'acl\s*=\s*"public-read(?:-write)?"'
		for match in re.finditer(pattern, content, re.MULTILINE):
			line_num = content[:match.start()].count('\n') + 1
			findings.append(IacFinding(
				rule_id='TF-002',
				rule_title='S3 bucket with public access',
				severity='critical',
				path=filepath,
				line=line_num,
				message="S3 bucket is publicly accessible via ACL",
				remediation="Use acl = 'private' and configure explicit bucket policies",
				references=['https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html'],
				engine="tf-scanner"
			))

		return findings

	def _scan_rds(self, content: str, filepath: str, lines: List[str]) -> List[IacFinding]:
		"""Scan for publicly accessible RDS instances."""
		findings = []

		pattern = r'publicly_accessible\s*=\s*true'
		for match in re.finditer(pattern, content, re.MULTILINE):
			line_num = content[:match.start()].count('\n') + 1
			findings.append(IacFinding(
				rule_id='TF-003',
				rule_title='RDS instance publicly accessible',
				severity='critical',
				path=filepath,
				line=line_num,
				message="RDS instance is accessible from the internet",
				remediation="Set publicly_accessible = false",
				references=['https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_SettingUp.html'],
				engine="tf-scanner"
			))

		return findings

	def _scan_iam(self, content: str, filepath: str, lines: List[str]) -> List[IacFinding]:
		"""Scan for overly permissive IAM policies."""
		findings = []

		# Check for wildcard actions
		pattern = r'"Action"\s*:\s*(?:\[\s*)?"\*"'
		for match in re.finditer(pattern, content, re.MULTILINE):
			line_num = content[:match.start()].count('\n') + 1
			findings.append(IacFinding(
				rule_id='TF-006',
				rule_title='IAM policy with wildcard actions',
				severity='high',
				path=filepath,
				line=line_num,
				message="IAM policy uses wildcard (*) for actions",
				remediation="Specify exact actions required instead of wildcards",
				references=['https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html'],
				engine="tf-scanner"
			))

		# Check for wildcard resources
		pattern = r'"Resource"\s*:\s*(?:\[\s*)?"\*"'
		for match in re.finditer(pattern, content, re.MULTILINE):
			line_num = content[:match.start()].count('\n') + 1
			findings.append(IacFinding(
				rule_id='TF-007',
				rule_title='IAM policy with wildcard resources',
				severity='high',
				path=filepath,
				line=line_num,
				message="IAM policy applies to all resources (*)",
				remediation="Specify exact resource ARNs",
				references=['https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html'],
				engine="tf-scanner"
			))

		return findings

	def _scan_encryption(self, content: str, filepath: str, lines: List[str]) -> List[IacFinding]:
		"""Scan for missing encryption settings."""
		findings = []

		# Check for unencrypted EBS volumes
		if 'aws_ebs_volume' in content and 'encrypted' not in content:
			findings.append(IacFinding(
				rule_id='TF-005',
				rule_title='EBS volume without encryption',
				severity='high',
				path=filepath,
				line=0,
				message="EBS volume may not be encrypted",
				remediation="Add encrypted = true to the resource",
				references=['https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/EBSEncryption.html'],
				engine="tf-scanner"
			))

		return findings

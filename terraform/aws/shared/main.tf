# One-time setup for p4n4 emulators on AWS: docs/guides/aws.md, section 3.
# The names are fixed, and the emulator module looks them up by name.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_vpc" "default" {
  default = true
}

locals {
  bucket = "p4n4-emu-${data.aws_caller_identity.current.account_id}"
}

# ── Bucket: reports/, projects/ and the emulators' Terraform state (tfstate/) ──

resource "aws_s3_bucket" "emu" {
  bucket = local.bucket
}

resource "aws_s3_bucket_public_access_block" "emu" {
  bucket                  = aws_s3_bucket.emu.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

# Versioning keeps earlier Terraform states recoverable
resource "aws_s3_bucket_versioning" "emu" {
  bucket = aws_s3_bucket.emu.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "emu" {
  bucket     = aws_s3_bucket.emu.id
  depends_on = [aws_s3_bucket_versioning.emu]

  rule {
    id     = "projects"
    status = "Enabled"
    filter {
      prefix = "projects/"
    }
    expiration {
      days = 7
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  rule {
    id     = "reports"
    status = "Enabled"
    filter {
      prefix = "reports/"
    }
    expiration {
      days = 180
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  # Current state files never expire; replaced versions go after 90 days
  rule {
    id     = "tfstate-history"
    status = "Enabled"
    filter {
      prefix = "tfstate/"
    }
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

# ── Instance role: SSM, and the bucket ─────────────────────────────────────────

resource "aws_iam_role" "instance" {
  name = "p4n4-emu-instance"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "bucket" {
  name = "bucket"
  role = aws_iam_role.instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["${aws_s3_bucket.emu.arn}/reports/*", "${aws_s3_bucket.emu.arn}/projects/*"]
      },
      {
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.emu.arn
      },
    ]
  })
}

resource "aws_iam_instance_profile" "instance" {
  name = "p4n4-emu-instance"
  role = aws_iam_role.instance.name
}

# ── Security group: no inbound, all outbound ───────────────────────────────────

resource "aws_security_group" "emu" {
  name        = "p4n4-emu"
  description = "p4n4 emulators: no inbound, SSM only"
  vpc_id      = data.aws_vpc.default.id

  # Terraform drops AWS's default allow-all egress rule, so it is declared here
  egress {
    description      = "SSM, S3, GitHub and image pulls"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
}

# ── Budget and cost allocation tags ────────────────────────────────────────────

resource "aws_budgets_budget" "monthly" {
  count = length(var.budget_emails) > 0 ? 1 : 0

  name         = "p4n4-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_amount)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.budget_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = var.budget_emails
  }
}

resource "aws_ce_cost_allocation_tag" "tags" {
  for_each = var.activate_cost_tags ? toset(["Project", "p4n4-run"]) : toset([])

  tag_key = each.key
  status  = "Active"
}

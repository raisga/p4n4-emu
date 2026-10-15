# One p4n4 emulator on EC2: docs/guides/aws.md, section 4.
# The workspace is the run: terraform workspace new rpi5-20261015-1430

locals {
  run = terraform.workspace

  # Profile → instance (guide 4.1). Larger than the board: p4n4-emu caps the
  # containers, and the OS, Docker and the SSM agent need room outside that cap.
  profiles = {
    "rpi-zero2w"       = { type = "c7g.xlarge", arch = "arm64", disk_gb = 40, throughput = null, gpu = false }
    "rpi3"             = { type = "c7g.xlarge", arch = "arm64", disk_gb = 40, throughput = null, gpu = false }
    "rpi4"             = { type = "c7g.xlarge", arch = "arm64", disk_gb = 40, throughput = null, gpu = false }
    "rpi5"             = { type = "m7g.xlarge", arch = "arm64", disk_gb = 40, throughput = null, gpu = false }
    "jetson-orin-nano" = { type = "g5g.2xlarge", arch = "arm64", disk_gb = 100, throughput = null, gpu = true }
    "mcu-class"        = { type = "t3.small", arch = "amd64", disk_gb = 40, throughput = null, gpu = false }
    "nuc"              = { type = "r7i.xlarge", arch = "amd64", disk_gb = 40, throughput = 250, gpu = false }
  }

  # rpi5-20261015-1430 → rpi5; no profile name is a prefix of another followed by "-"
  from_workspace = [for name in keys(local.profiles) : name if startswith(local.run, "${name}-")]
  profile        = coalesce(var.profile, one(local.from_workspace), "unknown")
  p              = lookup(local.profiles, local.profile, local.profiles["rpi5"])

  # The GPU profile uses AWS's Deep Learning AMI, which ships the NVIDIA driver,
  # the Container Toolkit and Docker (guide 4.4)
  gpu_ami       = "/aws/service/deeplearning/ami/arm64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
  ubuntu_ami    = "/aws/service/canonical/ubuntu/server/24.04/stable/current/${local.p.arch}/hvm/ebs-gp3/ami-id"
  ami_parameter = local.p.gpu ? local.gpu_ami : local.ubuntu_ami
}

data "aws_vpc" "default" {
  default = true
}

data "aws_security_group" "emu" {
  name   = "p4n4-emu"
  vpc_id = data.aws_vpc.default.id
}

data "aws_caller_identity" "current" {}

data "aws_ssm_parameter" "ami" {
  name = local.ami_parameter
}

resource "time_static" "created" {}

resource "aws_instance" "emu" {
  ami                  = data.aws_ssm_parameter.ami.insecure_value
  instance_type        = coalesce(var.instance_type, local.p.type)
  iam_instance_profile = "p4n4-emu-instance"

  vpc_security_group_ids = [data.aws_security_group.emu.id]

  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    ttl_minutes    = var.ttl_minutes
    install_docker = !local.p.gpu
    git_refs       = var.git_refs
  })
  user_data_replace_on_change = true

  # The TTL's shutdown terminates the instance, and with it the root volume
  instance_initiated_shutdown_behavior = "terminate"

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = coalesce(var.root_volume_gb, local.p.disk_gb)
    throughput            = local.p.throughput
    delete_on_termination = true
  }

  tags        = { Name = "p4n4-emu-${local.run}" }
  volume_tags = { Name = "p4n4-emu-${local.run}", Project = "p4n4", "p4n4-run" = local.run }

  lifecycle {
    # A newer Ubuntu image mustn't replace a running emulator
    ignore_changes = [ami]

    precondition {
      condition     = local.run != "default" && can(regex("^[a-z][a-z0-9-]{2,40}$", local.run))
      error_message = "Select a workspace named after the run, e.g. terraform workspace new rpi5-$(date +%Y%m%d-%H%M). \"${local.run}\" isn't one."
    }
    precondition {
      condition     = contains(keys(local.profiles), local.profile)
      error_message = "No profile found: name the workspace <profile>-<suffix> or set -var profile=<name>. Profiles: ${join(", ", keys(local.profiles))}."
    }
  }
}

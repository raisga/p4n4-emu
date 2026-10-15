# Plans against a mock provider: no AWS account needed.
# Run with the workspace as the run name: TF_WORKSPACE=rpi5-20261015-1430 terraform test

mock_provider "aws" {
  override_data {
    target = data.aws_ssm_parameter.ami
    values = { insecure_value = "ami-0123456789abcdef0" }
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012" }
  }
}

run "profile_from_workspace" {
  command = plan

  assert {
    condition     = local.profile == "rpi5" && aws_instance.emu.instance_type == "m7g.xlarge"
    error_message = "rpi5-… should plan an m7g.xlarge, got ${local.profile} / ${aws_instance.emu.instance_type}"
  }
  assert {
    condition     = strcontains(local.ami_parameter, "/24.04/stable/current/arm64/")
    error_message = "rpi5 should boot the arm64 Ubuntu image"
  }
  assert {
    condition     = aws_instance.emu.root_block_device[0].volume_size == 40 && aws_instance.emu.instance_initiated_shutdown_behavior == "terminate"
    error_message = "40 GB root volume, terminate on shutdown"
  }
  assert {
    condition     = strcontains(aws_instance.emu.user_data, "shutdown -P +480") && strcontains(aws_instance.emu.user_data, "get.docker.com")
    error_message = "cloud-init should schedule the TTL and install Docker"
  }
  assert {
    condition     = output.fetch_report == "aws s3 cp s3://p4n4-emu-123456789012/reports/rpi5-20261015-1430.tar.gz ."
    error_message = "fetch_report: ${output.fetch_report}"
  }
}

run "nuc_gets_disk_throughput" {
  command = plan
  variables {
    profile = "nuc"
  }

  assert {
    condition     = aws_instance.emu.instance_type == "r7i.xlarge" && aws_instance.emu.root_block_device[0].throughput == 250
    error_message = "nuc: r7i.xlarge with 250 MB/s"
  }
  assert {
    condition     = strcontains(local.ami_parameter, "/amd64/")
    error_message = "nuc should boot the amd64 image"
  }
}

run "gpu_uses_the_deep_learning_ami" {
  command = plan
  variables {
    profile = "jetson-orin-nano"
  }

  assert {
    condition     = aws_instance.emu.instance_type == "g5g.2xlarge" && aws_instance.emu.root_block_device[0].volume_size == 100
    error_message = "jetson: g5g.2xlarge with 100 GB"
  }
  assert {
    condition     = strcontains(local.ami_parameter, "deeplearning") && !strcontains(aws_instance.emu.user_data, "get.docker.com")
    error_message = "jetson: the Deep Learning AMI, which has Docker already"
  }
}

run "overrides" {
  command = plan
  variables {
    instance_type  = "t4g.xlarge"
    root_volume_gb = 60
    ttl_minutes    = 90
    git_refs       = { emu = "v0.2.0" }
  }

  assert {
    condition     = aws_instance.emu.instance_type == "t4g.xlarge" && aws_instance.emu.root_block_device[0].volume_size == 60
    error_message = "instance_type and root_volume_gb should override the profile"
  }
  assert {
    condition     = strcontains(aws_instance.emu.user_data, "shutdown -P +90") && strcontains(aws_instance.emu.user_data, "--branch v0.2.0 https://github.com/raisga/p4n4-emu") && strcontains(aws_instance.emu.user_data, "--branch main https://github.com/raisga/p4n4-lib")
    error_message = "ttl_minutes and git_refs should reach cloud-init"
  }
}

run "unknown_profile_fails" {
  command = plan
  variables {
    profile = "rpi9"
  }
  expect_failures = [aws_instance.emu]
}

run "ttl_out_of_range_fails" {
  command = plan
  variables {
    ttl_minutes = 5
  }
  expect_failures = [var.ttl_minutes]
}

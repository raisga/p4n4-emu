# One p4n4 emulator on Compute Engine: docs/guides/gcp.md, section 4.
# The workspace is the run: terraform workspace new rpi5-20261015-1430

locals {
  run = terraform.workspace

  # Profile → machine (guide 4.1). Larger than the board: p4n4-emu caps the
  # containers, and the OS and Docker need room outside that cap.
  profiles = {
    "rpi-zero2w"       = { type = "c4a-highcpu-4", arch = "arm64", disk = "hyperdisk-balanced", throughput = null, gpu = false }
    "rpi3"             = { type = "c4a-highcpu-4", arch = "arm64", disk = "hyperdisk-balanced", throughput = null, gpu = false }
    "rpi4"             = { type = "c4a-highcpu-4", arch = "arm64", disk = "hyperdisk-balanced", throughput = null, gpu = false }
    "rpi5"             = { type = "c4a-standard-4", arch = "arm64", disk = "hyperdisk-balanced", throughput = null, gpu = false }
    "jetson-orin-nano" = { type = "g2-standard-8", arch = "amd64", disk = "pd-balanced", throughput = null, gpu = true }
    "mcu-class"        = { type = "e2-small", arch = "amd64", disk = "pd-balanced", throughput = null, gpu = false }
    "nuc"              = { type = "n4-highmem-4", arch = "amd64", disk = "hyperdisk-balanced", throughput = 250, gpu = false }
  }

  # rpi5-20261015-1430 → rpi5; no profile name is a prefix of another followed by "-"
  from_workspace = [for name in keys(local.profiles) : name if startswith(local.run, "${name}-")]
  profile        = coalesce(var.profile, one(local.from_workspace), "unknown")
  p              = lookup(local.profiles, local.profile, local.profiles["rpi5"])
  disk_type      = coalesce(var.boot_disk_type, local.p.disk)
  # Only Hyperdisk takes a provisioned throughput
  throughput = local.disk_type == "hyperdisk-balanced" ? local.p.throughput : null
}

data "google_client_config" "current" {}

resource "time_static" "created" {}

resource "google_compute_instance" "emu" {
  name         = "p4n4-emu-${local.run}"
  machine_type = coalesce(var.machine_type, local.p.type)
  tags         = ["p4n4-emu"]

  boot_disk {
    auto_delete = true
    initialize_params {
      image                  = "ubuntu-os-cloud/ubuntu-2404-lts-${local.p.arch}"
      size                   = var.boot_disk_gb
      type                   = local.disk_type
      provisioned_throughput = local.throughput
      # Instance labels don't reach the boot disk: label it here for the billing data
      labels = { app = "p4n4", "p4n4-run" = local.run }
    }
  }

  network_interface {
    network = "p4n4-emu"
    access_config {} # an ephemeral external IP, for outbound traffic only
  }

  # No service_account block: the VM has none, so code on it can't call Google Cloud APIs

  metadata = {
    enable-oslogin = "TRUE"
    user-data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
      git_refs = var.git_refs
    })
  }

  scheduling {
    # The TTL: Google Cloud deletes the VM when the run time is up
    max_run_duration {
      seconds = var.ttl_minutes * 60
    }
    instance_termination_action = "DELETE"
    # GPU machines can't live-migrate
    on_host_maintenance = local.p.gpu ? "TERMINATE" : null
  }

  lifecycle {
    precondition {
      condition     = local.run != "default" && can(regex("^[a-z][a-z0-9-]{2,40}[a-z0-9]$", local.run))
      error_message = "Select a workspace named after the run, e.g. terraform workspace new rpi5-$(date +%Y%m%d-%H%M). \"${local.run}\" isn't one."
    }
    precondition {
      condition     = contains(keys(local.profiles), local.profile)
      error_message = "No profile found: name the workspace <profile>-<suffix> or set -var profile=<name>. Profiles: ${join(", ", keys(local.profiles))}."
    }
  }
}

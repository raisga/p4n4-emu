# Plans against a mock provider: no Google Cloud project needed.
# Run with the workspace as the run name: TF_WORKSPACE=rpi5-20261015-1430 terraform test

mock_provider "google" {
  override_data {
    target = data.google_client_config.current
    values = { project = "p4n4-emu-test" }
  }
}

run "profile_from_workspace" {
  command = plan

  assert {
    condition     = local.profile == "rpi5" && google_compute_instance.emu.machine_type == "c4a-standard-4"
    error_message = "rpi5-… should plan a c4a-standard-4, got ${local.profile} / ${google_compute_instance.emu.machine_type}"
  }
  assert {
    condition     = google_compute_instance.emu.name == "p4n4-emu-rpi5-20261015-1430"
    error_message = "VM name: ${google_compute_instance.emu.name}"
  }
  assert {
    condition     = google_compute_instance.emu.boot_disk[0].initialize_params[0].image == "ubuntu-os-cloud/ubuntu-2404-lts-arm64" && local.disk_type == "hyperdisk-balanced" && local.throughput == null
    error_message = "rpi5: arm64 image on hyperdisk-balanced, default throughput"
  }
  assert {
    condition     = google_compute_instance.emu.boot_disk[0].initialize_params[0].labels["p4n4-run"] == "rpi5-20261015-1430"
    error_message = "the boot disk should carry the run label"
  }
  assert {
    condition     = google_compute_instance.emu.scheduling[0].max_run_duration[0].seconds == 28800 && google_compute_instance.emu.scheduling[0].instance_termination_action == "DELETE"
    error_message = "TTL: 8 h, then delete"
  }
  assert {
    condition     = length(google_compute_instance.emu.service_account) == 0
    error_message = "the VM must have no service account"
  }
  assert {
    condition     = strcontains(google_compute_instance.emu.metadata["user-data"], "uid: 2000") && google_compute_instance.emu.metadata["enable-oslogin"] == "TRUE"
    error_message = "cloud-init creates the p4n4 user; OS Login on"
  }
}

run "nuc_gets_disk_throughput" {
  command = plan
  variables {
    profile = "nuc"
  }

  assert {
    condition     = google_compute_instance.emu.machine_type == "n4-highmem-4" && google_compute_instance.emu.boot_disk[0].initialize_params[0].provisioned_throughput == 250
    error_message = "nuc: n4-highmem-4 with 250 MB/s"
  }
}

run "throughput_only_on_hyperdisk" {
  command = plan
  variables {
    profile        = "nuc"
    machine_type   = "e2-standard-4"
    boot_disk_type = "pd-balanced"
  }

  assert {
    condition     = local.throughput == null
    error_message = "pd-balanced takes no provisioned throughput"
  }
}

run "gpu_terminates_on_maintenance" {
  command = plan
  variables {
    profile = "jetson-orin-nano"
  }

  assert {
    condition     = google_compute_instance.emu.machine_type == "g2-standard-8" && google_compute_instance.emu.scheduling[0].on_host_maintenance == "TERMINATE"
    error_message = "jetson: g2-standard-8, TERMINATE on host maintenance"
  }
  assert {
    condition     = google_compute_instance.emu.boot_disk[0].initialize_params[0].image == "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
    error_message = "jetson runs x86 here"
  }
}

run "unknown_profile_fails" {
  command = plan
  variables {
    profile = "rpi9"
  }
  expect_failures = [google_compute_instance.emu]
}

locals {
  project = data.google_client_config.current.project
  name    = google_compute_instance.emu.name
  zone    = google_compute_instance.emu.zone
  ssh     = "gcloud compute ssh ${local.name} --project ${local.project} --zone ${local.zone} --tunnel-through-iap"
}

output "run" {
  value = local.run
}

output "profile" {
  value = local.profile
}

output "vm" {
  value = local.name
}

output "machine_type" {
  value = google_compute_instance.emu.machine_type
}

output "ttl_expires" {
  description = "About when Google Cloud deletes the VM (max-run-duration from start)."
  value       = timeadd(time_static.created.rfc3339, "${var.ttl_minutes}m")
}

output "connect" {
  value = "${local.ssh}   # then: sudo -iu p4n4"
}

output "grafana" {
  value = "${local.ssh} -- -N -L 3000:localhost:3000"
}

output "report" {
  description = "Make the report on the VM and copy it back (guide 6.1)."
  value       = "${local.ssh} --command \"sudo -iu p4n4 bash -lc 'cd ~/greenhouse && p4n4-emu report -l run=${local.run} -l profile=${local.profile} -l machine-type=${google_compute_instance.emu.machine_type} -o /tmp/${local.run}.tar.gz'\" && gcloud compute scp --project ${local.project} --zone ${local.zone} --tunnel-through-iap ${local.name}:/tmp/${local.run}.tar.gz . && gcloud storage cp ${local.run}.tar.gz gs://${local.project}/reports/${local.run}.tar.gz"
}

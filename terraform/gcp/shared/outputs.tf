output "bucket" {
  value = google_storage_bucket.emu.name
}

output "backend_hcl" {
  description = "The emulator module's backend settings: terraform output -raw backend_hcl > ../emulator/backend.hcl"
  value       = <<-EOT
    bucket = "${google_storage_bucket.emu.name}"
    prefix = "tfstate/emulator"
  EOT
}

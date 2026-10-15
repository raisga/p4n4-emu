output "bucket" {
  value = aws_s3_bucket.emu.bucket
}

output "region" {
  value = data.aws_region.current.region
}

output "backend_hcl" {
  description = "The emulator module's backend settings: terraform output -raw backend_hcl > ../emulator/backend.hcl"
  value       = <<-EOT
    bucket               = "${aws_s3_bucket.emu.bucket}"
    key                  = "emulator.tfstate"
    workspace_key_prefix = "tfstate"
    region               = "${data.aws_region.current.region}"
    use_lockfile         = true
  EOT
}

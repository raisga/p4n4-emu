locals {
  bucket = "p4n4-emu-${data.aws_caller_identity.current.account_id}"
}

output "run" {
  value = local.run
}

output "profile" {
  value = local.profile
}

output "instance_id" {
  value = aws_instance.emu.id
}

output "instance_type" {
  value = aws_instance.emu.instance_type
}

output "ttl_expires" {
  description = "About when the instance terminates itself (TTL from first boot)."
  value       = timeadd(time_static.created.rfc3339, "${var.ttl_minutes}m")
}

output "connect" {
  value = "aws ssm start-session --target ${aws_instance.emu.id}   # then: sudo -iu ubuntu"
}

output "grafana" {
  value = "aws ssm start-session --target ${aws_instance.emu.id} --document-name AWS-StartPortForwardingSession --parameters '{\"portNumber\":[\"3000\"],\"localPortNumber\":[\"3000\"]}'"
}

output "report" {
  description = "Run on the instance as ubuntu, in the project directory (guide 6.1)."
  value       = "p4n4-emu report -l run=${local.run} -l profile=${local.profile} -l instance-type=${aws_instance.emu.instance_type} -o /tmp/${local.run}.tar.gz && aws s3 cp /tmp/${local.run}.tar.gz s3://${local.bucket}/reports/${local.run}.tar.gz"
}

output "fetch_report" {
  value = "aws s3 cp s3://${local.bucket}/reports/${local.run}.tar.gz ."
}

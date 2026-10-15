variable "region" {
  description = "Region of the run. Default: the AWS CLI's region (AWS_REGION or the profile's)."
  type        = string
  default     = null
}

variable "profile" {
  description = "p4n4-emu profile. Default: read from the workspace name, <profile>-<suffix>, e.g. rpi5-20261015-1430."
  type        = string
  default     = null
}

variable "instance_type" {
  description = "Override the profile's instance type, e.g. t4g.xlarge for a cheaper smoke test."
  type        = string
  default     = null
}

variable "root_volume_gb" {
  description = "Override the root volume size."
  type        = number
  default     = null
}

variable "ttl_minutes" {
  description = "The instance terminates itself this long after first boot. Take the report before then."
  type        = number
  default     = 480

  validation {
    condition     = var.ttl_minutes >= 30 && var.ttl_minutes <= 10080
    error_message = "ttl_minutes must be between 30 minutes and 7 days."
  }
}

variable "git_refs" {
  description = "Branch or tag to clone of p4n4-emu, p4n4-lib and p4n4-templates."
  type = object({
    emu       = optional(string, "main")
    lib       = optional(string, "main")
    templates = optional(string, "main")
  })
  default = {}
}

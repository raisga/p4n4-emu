variable "project" {
  description = "The emulator project's ID. Default: gcloud's project (GOOGLE_PROJECT or the active configuration)."
  type        = string
  default     = null
}

variable "zone" {
  description = "Zone of the run; it must offer the machine type (guide 4.1)."
  type        = string
  default     = "us-central1-a"
}

variable "profile" {
  description = "p4n4-emu profile. Default: read from the workspace name, <profile>-<suffix>, e.g. rpi5-20261015-1430."
  type        = string
  default     = null
}

variable "machine_type" {
  description = "Override the profile's machine type, e.g. e2-standard-4 for a cheaper smoke test (with boot_disk_type pd-balanced)."
  type        = string
  default     = null
}

variable "boot_disk_type" {
  description = "Override the profile's boot disk type. C4A and N4 take only hyperdisk-balanced."
  type        = string
  default     = null
}

variable "boot_disk_gb" {
  description = "Boot disk size."
  type        = number
  default     = 40
}

variable "ttl_minutes" {
  description = "Google Cloud deletes the VM, and its boot disk, this long after it starts. Take the report before then."
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

variable "subscription_id" {
  description = "The emulator subscription. Default: ARM_SUBSCRIPTION_ID."
  type        = string
  default     = null
}

variable "location" {
  description = "Region of the run; it must offer the size (guide 4.1)."
  type        = string
  default     = "eastus"
}

variable "profile" {
  description = "p4n4-emu profile. Default: read from the workspace name, <profile>-<suffix>, e.g. rpi5-20261015-1430."
  type        = string
  default     = null
}

variable "size" {
  description = "Override the profile's VM size, e.g. Standard_B4ps_v2 for a cheaper smoke test."
  type        = string
  default     = null
}

variable "os_disk_gb" {
  description = "Override the profile's OS disk size. Premium SSD throughput grows with size (guide 4.1)."
  type        = number
  default     = null
}

variable "ssh_public_key_path" {
  description = "Public key for the p4n4 admin user."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "ssh_source_cidr" {
  description = "Where SSH may come from. Default: this machine's public IPv4 address, /32."
  type        = string
  default     = null
}

variable "ttl_minutes" {
  description = "The VM deletes its own resource group this long after first boot. Take the report before then."
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

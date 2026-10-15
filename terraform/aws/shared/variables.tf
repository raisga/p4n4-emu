variable "region" {
  description = "Home region for the bucket and the emulators. Default: the AWS CLI's region (AWS_REGION or the profile's)."
  type        = string
  default     = null
}

variable "budget_amount" {
  description = "Monthly budget in USD. The budget is only created when budget_emails isn't empty."
  type        = number
  default     = 50
}

variable "budget_emails" {
  description = "Who gets the budget's alerts (80 % actual, 100 % forecast). Empty: no budget."
  type        = list(string)
  default     = []
}

variable "activate_cost_tags" {
  description = "Activate Project and p4n4-run as cost allocation tags. AWS only lists a tag key after a resource has carried it, so set this after the first run."
  type        = bool
  default     = false
}

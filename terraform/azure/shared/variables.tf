variable "subscription_id" {
  description = "The emulator subscription. Default: ARM_SUBSCRIPTION_ID."
  type        = string
  default     = null
}

variable "location" {
  description = "Region for the shared resource group and the storage account."
  type        = string
  default     = "eastus"
}

variable "budget_amount" {
  description = "Monthly budget in USD for the subscription. The budget is only created when budget_emails isn't empty."
  type        = number
  default     = 50
}

variable "budget_emails" {
  description = "Who gets the budget's alerts (80 % actual, 100 % forecast). Empty: no budget."
  type        = list(string)
  default     = []
}

variable "project" {
  description = "The emulator project's ID (guide 2.4), e.g. p4n4-emu-4f2a9c."
  type        = string
}

variable "region" {
  description = "Region for the bucket and the dataset."
  type        = string
  default     = "us-central1"
}

variable "billing_account" {
  description = "Billing account ID (0X0X0X-0X0X0X-0X0X0X), for the budget. Null: no budget."
  type        = string
  default     = null
}

variable "budget_amount" {
  description = "Monthly budget in USD for the project."
  type        = number
  default     = 50
}

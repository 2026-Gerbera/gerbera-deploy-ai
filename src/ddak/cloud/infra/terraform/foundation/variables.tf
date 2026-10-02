variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "12 digit account ID required."
  }
}
variable "project" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,39}$", var.project))
    error_message = "Invalid project name."
  }
}
variable "state_bucket" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.state_bucket))
    error_message = "Invalid state bucket name."
  }
}

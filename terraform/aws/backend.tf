terraform {
  backend "s3" {
    bucket       = "sandiego-city-gis-opencontext-tfstate"
    key          = "terraform.tfstate"
    region       = "us-west-2"
    use_lockfile = true # S3-native state locking (replaces deprecated dynamodb_table)
    encrypt      = true
  }
}

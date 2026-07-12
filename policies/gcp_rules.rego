package terraform.gcp

deny contains msg if {
    resource := input.resource_changes[_]
    resource.type == "google_storage_bucket"
    resource.change.after != null
    not resource.change.after.uniform_bucket_level_access

    msg := sprintf(
        "security-risk: Bucket '%v' must have uniform bucket level access enabled.",
        [resource.name]
    )
}

deny contains msg if {
    resource := input.resource_changes[_]
    resource.change.after != null
    resource.change.after.region != "us-central1"

    msg := sprintf(
        "compliance: Resource '%v' must be in us-central1.",
        [resource.name]
    )
}

deny contains msg if {
    resource := input.resource_changes[_]
    resource.type == "google_compute_instance"
    resource.change.after != null

    interfaces := resource.change.after.network_interface
    some i
    interfaces[i].access_config
    count(interfaces[i].access_config) > 0

    msg := sprintf(
        "security-risk: Instance '%v' is configured with a public IP. This is prohibited by policy.",
        [resource.name]
    )
}

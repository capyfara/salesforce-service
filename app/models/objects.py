from pydantic import BaseModel

class SupportedObject(BaseModel):
    name: str 
    label: str
    soql_template: str
    default_fields: list[str]
    supports_incremental: bool
    base_where: str | None = None

SUPPORTED_OBJECTS: list[SupportedObject] = [
    SupportedObject (
        name = "Contact",
        label = "Contacts",
        soql_template = "SELECT {fields} FROM Contact {where_clause} ORDER BY LastModifiedDate ASC",
        default_fields = ["Id", "Name", "Email", "Phone", "AccountId", "OwnerId", "CreatedDate", "LastModifiedDate"],
        supports_incremental = True
    ), 
    SupportedObject (
        name = "Account",
        label = "Accounts",
        soql_template = "SELECT {fields} FROM Account {where_clause} ORDER BY LastModifiedDate ASC",
        default_fields = ["Id", "Name", "Type", "Industry", "BillingCity", "BillingState", "OwnerId", "CreatedDate", "LastModifiedDate"],
        supports_incremental = True
    ),
    SupportedObject (
        name = "Opportunity",
        label = "Opportunities",
        soql_template = "SELECT {fields} FROM Opportunity {where_clause} ORDER BY LastModifiedDate ASC",
        default_fields = ["Id", "Name", "AccountId", "OwnerId", "StageName", "Amount", "CloseDate", "CreatedDate", "LastModifiedDate"],
        supports_incremental = True
    ),
    SupportedObject(
        name="Lead",
        label="Leads",
        soql_template="SELECT {fields} FROM Lead {where_clause} ORDER BY LastModifiedDate ASC",
        default_fields=["Id", "FirstName", "LastName", "Email", "Phone", "Company", "Status",
                        "LeadSource", "IsConverted", "ConvertedDate", "ConvertedAccountId",
                        "ConvertedContactId", "ConvertedOpportunityId", "OwnerId",
                        "CreatedDate", "LastModifiedDate"],
        supports_incremental=True,
    ),
    SupportedObject(
        name="User",
        label="Users",
        soql_template="SELECT {fields} FROM User {where_clause} ORDER BY LastModifiedDate ASC",
        default_fields=["Id", "Name", "Email", "Username", "Title", "IsActive", "ProfileId",
                        "UserRoleId", "ManagerId", "CreatedDate", "LastModifiedDate"],
        supports_incremental=True,
        base_where="IsActive = true",
    ),
    SupportedObject(
        name="CampaignMember",
        label="Campaign Members",
        soql_template="SELECT {fields} FROM CampaignMember {where_clause} ORDER BY LastModifiedDate ASC",
        default_fields=["Id", "CampaignId", "LeadId", "ContactId", "Status", "HasResponded",
                        "FirstRespondedDate", "CreatedDate", "LastModifiedDate"],
        supports_incremental=True,
    ),
    SupportedObject(
        name="Task",
        label="Tasks",
        soql_template="SELECT {fields} FROM Task {where_clause} ORDER BY LastModifiedDate ASC",
        default_fields=["Id", "Subject", "Status", "Priority", "ActivityDate", "IsClosed",
                        "TaskSubtype", "WhoId", "WhatId", "AccountId", "OwnerId",
                        "CreatedDate", "LastModifiedDate"],
        supports_incremental=True,
    ),
    SupportedObject(
        name="Event",
        label="Events",
        soql_template="SELECT {fields} FROM Event {where_clause} ORDER BY LastModifiedDate ASC",
        default_fields=["Id", "Subject", "StartDateTime", "EndDateTime", "ActivityDate",
                        "IsAllDayEvent", "EventSubtype", "WhoId", "WhatId", "AccountId",
                        "OwnerId", "CreatedDate", "LastModifiedDate"],
        supports_incremental=True,
    ),
]
from pydantic import BaseModel

class SupportedObject(BaseModel):
    name: str 
    label: str
    soql_template: str
    default_fields: list[str]
    supports_incremental: bool

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
    )
    # Add Activity, Lead, User, CampaignMember per Section 3.3 —
    # confirm exact default_fields with your manager if not specified,
    # since the doc doesn't give explicit field lists for these 4.
]
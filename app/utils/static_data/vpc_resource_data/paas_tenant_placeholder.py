ASPORA_TENANT_PLACEHOLDER = {
  "geoLocations": [
    {
      "id": "geo-india",
      "name": "India",
      "displayName": "India",
      "position": {
        "x": 0,
        "y": 0
      },
      "settings": {}
    },
  ],
  "accounts": [
    {
      "id": "account-000000000000-geo-india",
      "accountId": "000000000000",
      "vendor": "AWS",
      "geoLocationId": "geo-india",
      "position": {
        "x": 30,
        "y": 48
      },
      "settings": {}
    },
  ],
  "cloudRegions": [
    {
      "id": "cr-000000000000-ap-south-1",
      "name": "ap-south-1",
      "displayName": "ap-south-1 (Mumbai)",
      "accountId": "account-000000000000-geo-india",
      "settings": {}
    },
  ],
  "vpcs": [
    {
      "id": "vpc-placeholder-main",
      "name": "main-vpc",
      "cloudRegionId": "cr-000000000000-ap-south-1",
      "cidr": "10.0.0.0/16",
      "settings": {}
    },
  ],
  "subnets": [
    {
      "id": "subnet-placeholder-public-1a",
      "name": "",
      "az": "ap-south-1a",
      "type": "public",
      "cidr": "10.0.1.0/24",
      "vpcId": "vpc-placeholder-main",
      "displayOrder": 0
    },
    {
      "id": "subnet-placeholder-public-1b",
      "name": "",
      "az": "ap-south-1b",
      "type": "public",
      "cidr": "10.0.2.0/24",
      "vpcId": "vpc-placeholder-main",
      "displayOrder": 1
    },
    {
      "id": "subnet-placeholder-private-1a",
      "name": "",
      "az": "ap-south-1a",
      "type": "private",
      "cidr": "10.0.3.0/24",
      "vpcId": "vpc-placeholder-main",
      "displayOrder": 2
    },
    {
      "id": "subnet-placeholder-private-1b",
      "name": "",
      "az": "ap-south-1b",
      "type": "private",
      "cidr": "10.0.4.0/24",
      "vpcId": "vpc-placeholder-main",
      "displayOrder": 3
    },
  ],
  "ecsClusters": [],
  "eksClusters": [],
  "nodes": [],
  "nodeVariables": {},
  "variableRefs": {},
  "securityGroupRules": []
}

PAAS_TENANT_PLACEHOLDER = {
  "geoLocations": [
    {
      "id": "geo-us",
      "name": "US",
      "displayName": "US",
      "position": {
        "x": 0,
        "y": 0
      },
      "settings": {}
    },
  ],
  "accounts": [
    {
      "id": "account-000000000000-geo-us",
      "accountId": "000000000000",
      "vendor": "AWS",
      "geoLocationId": "geo-us",
      "position": {
        "x": 30,
        "y": 48
      },
      "settings": {}
    },
  ],
  "cloudRegions": [
    {
      "id": "cr-000000000000-us-east-1",
      "name": "us-east-1",
      "displayName": "us-east-1 (Virginia)",
      "accountId": "account-000000000000-geo-us",
      "settings": {}
    },
  ],
  "vpcs": [
    {
      "id": "vpc-placeholder-main",
      "name": "main-vpc",
      "cloudRegionId": "cr-000000000000-us-east-1",
      "cidr": "10.0.0.0/16",
      "settings": {}
    },
  ],
  "subnets": [
    {
      "id": "subnet-placeholder-public-1a",
      "name": "",
      "az": "us-east-1a",
      "type": "public",
      "cidr": "10.0.1.0/24",
      "vpcId": "vpc-placeholder-main",
      "displayOrder": 0
    },
    {
      "id": "subnet-placeholder-public-1b",
      "name": "",
      "az": "us-east-1b",
      "type": "public",
      "cidr": "10.0.2.0/24",
      "vpcId": "vpc-placeholder-main",
      "displayOrder": 1
    },
    {
      "id": "subnet-placeholder-private-1a",
      "name": "",
      "az": "us-east-1a",
      "type": "private",
      "cidr": "10.0.3.0/24",
      "vpcId": "vpc-placeholder-main",
      "displayOrder": 2
    },
    {
      "id": "subnet-placeholder-private-1b",
      "name": "",
      "az": "us-east-1b",
      "type": "private",
      "cidr": "10.0.4.0/24",
      "vpcId": "vpc-placeholder-main",
      "displayOrder": 3
    },
  ],
  "ecsClusters": [],
  "eksClusters": [],
  "nodes": [],
  "nodeVariables": {},
  "variableRefs": {},
  "securityGroupRules": []
}

from pydantic import BaseModel
from typing import Optional

class LocationData(BaseModel):
    city: Optional[str] = None
    area: Optional[str] = None
    address: Optional[str] = None

class PropertyDetailsData(BaseModel):
    property_type: Optional[str] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    area_sqft: Optional[float] = None

class AgentData(BaseModel):
    name: Optional[str] = None
    agency: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    image: Optional[str] = None

class Listing(BaseModel):
    listing_id: str
    price: float
    currency: str = "AED"
    location: LocationData
    property_details: PropertyDetailsData
    agent: AgentData
    url: str
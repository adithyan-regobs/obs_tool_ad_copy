from app.db.models.base_model import BaseModel

class AlertTypeRefModel(BaseModel):
    """
    Alert Type Reference table.
    Stores different types of alerts (e.g., 'http_4xx_rate', 'cpu_util').
    """

    __tablename__ = "alerttype_ref"

    def __repr__(self):
        return (
            f"<AlertTypeRef(id={self.id}, code='{self.code}', "
            f"name='{self.name}')>"
        )
from app.schemas.alert_schemas import CreateAlert

class AlertConfigValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors

    @staticmethod
    def validate_obs_create_alerts_rule(data: CreateAlert):
        errors: list[str] = []
        #TODO: add the rules

        # Example validation rules (add more as needed)
        # if alert.threshold_value <= 0:
        #     errors.append("Threshold value must be greater than 0")

        if errors:
            raise AlertConfigValidationError(errors)

    # @staticmethod
    # def validate_obs_get_alert_rule(data: GetAlertPolicy):
    #     errors: list[str] = []
    #     #TODO: add the rules

    #     if errors:
    #         raise AlertConfigValidationError(errors)
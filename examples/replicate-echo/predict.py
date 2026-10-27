from cog import BasePredictor, Input


class Predictor(BasePredictor):
    def predict(self, text: str = Input(default="ping")) -> str:
        return text

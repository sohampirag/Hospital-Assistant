from pipecat.frames.frames import Frame

class UserTurnFrame(Frame):
    def __init__(self,text:str):
        super().__init__()
        self.text=text
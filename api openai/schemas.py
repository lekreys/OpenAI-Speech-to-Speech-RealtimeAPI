from pydantic import BaseModel




class Conversation(BaseModel):

    id_conversation : str
    user_message: str
    agent_message : str

    class Config:
        orm_mode = True

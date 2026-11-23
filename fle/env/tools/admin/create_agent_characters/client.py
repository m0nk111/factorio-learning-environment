from fle.env.tools import Tool


class CreateAgentCharacters(Tool):
    def __init__(self, connection, game_state):
        super().__init__(connection, game_state)

    def __call__(self, num_agents: int, own_world: bool = True) -> bool:
        """
        Creates an agent character.

        own_world says whether this session owns the world it is attached to. A
        session that merely attached to someone else's world passes False: it may
        add a character, but it must not remove anyone else's.
        """
        response, elapsed = self.execute(num_agents, own_world)
        return True

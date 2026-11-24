FLE = FLE or {}
FLE.actions = FLE.actions or {}

-- Function to convert HSV to RGB
local function hsv_to_rgb(h, s, v)
    local r, g, b
    local i = math.floor(h * 6)
    local f = h * 6 - i
    local p = v * (1 - s)
    local q = v * (1 - f * s)
    local t = v * (1 - (1 - f) * s)
    
    i = i % 6
    
    if i == 0 then r, g, b = v, t, p
    elseif i == 1 then r, g, b = q, v, p
    elseif i == 2 then r, g, b = p, v, t
    elseif i == 3 then r, g, b = p, q, v
    elseif i == 4 then r, g, b = t, p, v
    elseif i == 5 then r, g, b = v, p, q
    end
    
    return {r = r, g = g, b = b, a = 1.0}
end

-- Function to generate a color based on the agent index
local function generate_agent_color(index, total_agents)
    local hue = (index - 1) / total_agents
    local saturation = 1.0
    local value = 1.0
    return hsv_to_rgb(hue, saturation, value)
end

-- Characters a human is playing. This script used to destroy every character on
-- the surface, which on a world someone is playing means their character: they
-- come back as a spectator and cannot act. Their character is theirs.
local function characters_played_by_humans()
    local keep = {}
    for _, player in pairs(game.players) do
        if player.character and player.character.valid then
            keep[player.character] = true
        end
    end
    return keep
end

local function human_is_playing()
    return #game.connected_players > 0
end

-- Create agent characters script.
--
-- own_world says whether this session owns the world it is attached to. On a
-- world that is not ours, nothing is destroyed: a character belongs to whoever
-- plays it, and an offline player cannot be recognised from game.players, so
-- there is no safe subset to delete. Adding a character is enough to work.
FLE.actions.create_agent_characters = function(num_agents, own_world)
    local own = own_world ~= false
    local keep = characters_played_by_humans()

    if own then
        -- Destroy existing agent characters if they exist, and only those.
        -- Anything not owned by a player is leftover scenery from a session
        -- that never cleaned up.
        if storage.agent_characters then
            for _, char in pairs(storage.agent_characters) do
                if char and char.valid then
                    char.destroy()
                end
            end
            storage.agent_characters = {}
        end
        for _, entity in pairs(game.surfaces[1].find_entities_filtered{type = "character"}) do
            if entity.valid and not keep[entity] then
                entity.destroy()
            end
        end
    elseif storage.agent_characters then
        -- Keep our own previous characters, but never touch anyone else's.
        for _, char in pairs(storage.agent_characters) do
            if char and char.valid then
                char.destroy()
            end
        end
        storage.agent_characters = {}
    end
    
    -- Create new characters for each agent
    for i = 1, num_agents do
        local char = game.surfaces[1].create_entity{
            name = "character",
            position = {x = 0, y = (i - 1) * 2},
            force = game.forces.player
        }
        
        -- Set colors for multi-agent scenarios
        if num_agents > 1 then
            char.color = generate_agent_color(i, num_agents)
        end
        
        storage.agent_characters[i] = char
    end

    -- Set the first character as the main player
    player = storage.agent_characters[1]
    -- always_day is a world setting. On a world that is not ours it is their
    -- day/night cycle, not ours to flatten.
    if own and not human_is_playing() then
        player.surface.always_day = true
    end
end
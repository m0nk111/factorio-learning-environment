FLE = FLE or {}
FLE.utils = FLE.utils or {}
FLE.actions = FLE.actions or {}
FLE.actions.get_prototype_recipe = function(player_index, recipe_name)
    local player = storage.agent_characters[player_index]
    local recipe = player.force.recipes[recipe_name]
    if not recipe then
        return "recipe doesnt exist"
    end
    local serialized = FLE.utils.serialize_recipe(recipe)
    return serialized
end


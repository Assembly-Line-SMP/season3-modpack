ServerEvents.recipes(event => {
    const transitional = 'create:incomplete_precision_mechanism' // Making a constant to store the transitional item makes the code more readable
    event.recipes.create.sequenced_assembly(
        // Outputs:
        [
            CreateItem.of('minecraft:elytra')
        ],
        // Input:
        'create:white_sail',
        // Sequence:
        [
            event.recipes.create.filling(transitional, [Fluid.of('aeronautics:levitite_blend'), transitional]),
            event.recipes.create.filling(transitional, [Fluid.of('petrochem:plastic'), transitional]),
            event.recipes.create.cutting(transitional, transitional).processingTime(200),
            event.recipes.create.filling(transitional, [Fluid.of('create_dragons_plus:dragon_breath'), transitional]),
            event.recipes.create.deploying(transitional, [transitional, 'minecraft:slime_ball']),
        ]
    )
    .transitionalItem(transitional) // Set the transitional item
    .loops(4) // Set the number of loops
 
});
